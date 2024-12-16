import json
import random

from django.http import JsonResponse
from django.http import HttpResponse
from django.views import View
from django.shortcuts import get_object_or_404
from django.db.models import F, Q
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .utils import validate_field, validate_amount_players, validate_integer_field

from .models import Room, roomTypes, RoomStatus
from players.models import Player, playerColors

class RoomGetView(View):
    def get(self, request):
        try:
            current_page = int(request.GET.get('currentPage', 1))
        except ValueError:
            current_page = 1
        page_size = int(request.GET.get('pageSize', 10))
        filter_label = request.GET.get('filterLabel', '')

        rooms = Room.objects.filter(privateRoom=False, amountOfPlayers__lt=F('maxAmountOfPlayers'), status__lt=RoomStatus.READY_FOR_START.value).order_by('name')
        if filter_label:
            rooms = rooms.filter(
                Q(name__icontains=filter_label) | Q(code__icontains=filter_label)
            )

        paginator = Paginator(rooms, page_size)
        try:
            paginated_rooms = paginator.page(current_page)
        except PageNotAnInteger:
            paginated_rooms = paginator.page(1)
        except EmptyPage:
            paginated_rooms = paginator.page(paginator.num_pages)

        data = [
            {
                "roomCode": room.code,
                "amountOfPlayers": room.amountOfPlayers,
                "maxAmountOfPlayers": room.maxAmountOfPlayers,
                "roomName": room.name,
                "roomType": room.type
            }
            for room in paginated_rooms
        ]

        total_items = rooms.count()
        total_pages = (total_items + page_size - 1) // page_size

        response = {
            "paginatedItems": {
                "currentPage": paginated_rooms.number,
                "pageSize": page_size,
                "nextPage": paginated_rooms.next_page_number() if paginated_rooms.has_next() else None,
                "previousPage": paginated_rooms.previous_page_number() if paginated_rooms.has_previous() else None,
                "hasNextPage": paginated_rooms.has_next(),
                "hasPreviousPage": paginated_rooms.has_previous(),
                "totalPages": total_pages,
                "Data": data
            }
        }

        return JsonResponse(response)

def setPlayerColor(room_code):
    room = Room.objects.filter(code=room_code).first()
    profileColor = 0
    used_colors = Player.objects.filter(roomCode=room_code).values_list('profileColor', flat=True)
    all_colors = {color.value for color in playerColors}
    available_colors = all_colors - set(used_colors)
    if available_colors:
        profileColor = available_colors.pop()
    return profileColor


class CreateRoomView(View):
    def post(self, request, *args, **kwargs):
        if not request.body or request.body.strip() == b'':
            return JsonResponse({'errorCode': '402', 'message': 'Bad Request'}, status=400)
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'errorCode': '401', 'message': 'Bad Request'}, status=400)

        try:
            created_by = validate_field(data, "createdBy", str)
            room_name = validate_field(data, "roomName", str)
            room_type = validate_integer_field(data, "roomType", default=0, required=True)
            private_room = validate_field(data, "privateRoom", bool, default=False, required=False)
            max_amount_of_players = validate_amount_players(data, "maxAmountOfPlayers", int, room_type)

            if (created_by == "" or room_name == ""):
                return JsonResponse({'errorCode': '400', 'message': 'createdBy and roomName fields are mandatory.'}, status=400)

        except (ValueError, TypeError) as e:
            return JsonResponse({'errorCode': '400', 'message': f"{e}."}, status=400)

        new_room = Room.objects.create(
            name=room_name,
            type=room_type,
            maxAmountOfPlayers=max_amount_of_players,
            privateRoom=private_room,
        )

        new_player = Player.objects.create(
            name=created_by,
            roomId=new_room,
            roomCode=new_room.code,
            profileColor=setPlayerColor(new_room.code),
            urlProfileImage=f"/assets/img/{random.choice([1, 2])}.png"
        )

        new_room.createdBy = new_player.id
        new_player.bracketsPosition = 0
        new_room.amountOfPlayers += 1

        if (new_room.maxAmountOfPlayers == 1):
            Player.objects.create(
                name="Bot",
                roomId=new_room,
                roomCode=new_room.code,
                profileColor=setPlayerColor(new_room.code),
                urlProfileImage=f"/assets/img/{random.choice([1, 2])}.png"
            )
            new_room.maxAmountOfPlayers += 1

        new_room.save()

        return JsonResponse(
            {'roomCode': new_room.code,
             'roomType': new_room.type},
            status=201,
            headers={
                'Location': f'/session/rooms/{new_room.code}',
                'X-User-Id': new_room.createdBy,
            }
        )

class RoomView(View):
    def delete(self, request, room_code):
        userId = request.headers.get("X-User-Id")
        if userId is None:
            return JsonResponse({'errorCode': '401', 'message': 'Unauthorized'}, status=401)
        
        room = Room.objects.filter(code=room_code).first()
        if room is None:
            return JsonResponse({}, status=204)
        
        players = Player.objects.filter(roomCode=room_code)
        user = players.filter(id=userId).first()
        if room.createdBy != user.id:
            return JsonResponse({'errorCode': '403', 'message': 'Forbidden'}, status=403)
        
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"room_{room_code}",
            { "type": "delete_room" }
        )
        players.delete()
        room.delete()
        return JsonResponse({}, status=200)

    def get(self, request, room_code):
        try:
            room = Room.objects.get(code=room_code)
            userId = request.headers.get("X-User-Id")
            user = Player.objects.filter(roomCode=room.code, id=userId).first()
            if user is None:
                return JsonResponse({'errorCode': '403', 'message': 'Forbidden'}, status=403)
            
            players = Player.objects.filter(roomCode=room.code)
            players_data = [
                {
                    'id': player.id if user.id == room.createdBy else None,
                    'name': player.name,
                    'profileColor': player.profileColor,
                    'urlProfileImage': player.urlProfileImage,
                    "owner": player.id == room.createdBy,
                }
                for player in players
            ]
            return JsonResponse(
                {
                    'roomId': room.id,
                    'roomType': room.type,
                    'roomCode': room.code,
                    'roomName': room.name,
                    'maxAmountOfPlayers': room.maxAmountOfPlayers,
                    'amountOfPlayers': len(players_data),
                    'createdBy': room.createdBy,
                    'players': players_data,
                    'isOwner': user.id == room.createdBy,
                }
            )
        except Room.DoesNotExist:
            return JsonResponse({'errorCode': '404', 'message': 'Room not found'}, status=404)

class RoomStatusView(View):
    def get(self, request, room_code):
        try:
            room = Room.objects.get(code=room_code)
            return JsonResponse({'status': str(room.status)})
        except Room.DoesNotExist:
            return JsonResponse({'errorCode': '404', 'message': 'Room status not found'}, status=404)

def update_players_list(room_code, userRemoved):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"room_{room_code}",
        {
            "type": "player_list_update",
            "userRemoved": userRemoved,
        }
    )

class AddPlayerToRoomView(View):
    def put(self, request, room_code):

        if not request.body or request.body.strip() == b'':
            return JsonResponse({'errorCode': '402', 'message': 'Bad Request'}, status=400)
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'errorCode': '401', 'message': 'Bad Request'}, status=400)
        
        try:
            player_name = validate_field(data, "playerName", str)
        except (ValueError, TypeError) as e:
            return JsonResponse({'errorCode': '400', 'message': f'{e}'}, status=400)

        try:
            room = Room.objects.filter(code=room_code).first()
            if room is None:
                return JsonResponse({'errorCode': '403', 'message': 'Room dont exist.'}, status=403)

            if room.players.count() >= room.maxAmountOfPlayers:
                return JsonResponse({'errorCode': '403', 'message': 'Room is full'}, status=403)
            
            player = Player.objects.create(
                name=player_name,
                roomCode=room_code,
                roomId=room,
                profileColor=setPlayerColor(room.code),
                urlProfileImage=f"/assets/img/{random.choice([1, 2])}.png"
            )
            room.amountOfPlayers += 1
            room.save()
            update_players_list(room_code, "")

            return JsonResponse(
                {
                    'roomCode': room.code,
                    'roomType': room.type
                },
                status=201,
                headers={
                    'Location': f'/session/rooms/{room_code}',
                    'X-User-Id': player.id
                }
            )
        except Room.DoesNotExist:
            return JsonResponse({'errorCode': '404', 'message': 'Room not found'}, status=404)

class RemovePlayerView(View):
    def delete(self, request, room_code, player_id):
        try:
            room = Room.objects.get(code=room_code)
        except Room.DoesNotExist:
            return JsonResponse({'errorCode': '404', 'message': 'Room not found'}, status=404)

        if room.type is roomTypes.SINGLE_PLAYER:
            return JsonResponse({'errorCode': '400', 'message': 'Bad request'}, status=400)

        try:
            player = Player.objects.get(id=player_id, roomCode=room_code)
        except Player.DoesNotExist:
            return JsonResponse({"errorCode": "404", "message": "Player not found in the room"}, status=404)

        update_players_list(room_code, player.id)

        player.delete()
        room.amountOfPlayers -= 1
        room.save()

        return HttpResponse(
            status=204,
            headers={
                'Location': f'/session/rooms/{room_code}/{player_id}',
                'playerId': player_id
            }
        )