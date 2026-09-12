# CONTEXT.md — User-Session

Contexto **Sessão/Lobby**: tudo que acontece antes e em volta da partida — salas,
jogadores, chaveamento de torneio e a orquestração que liga sala a jogo.

> Submodule de [Transcendence](https://github.com/Meia-noite-eu-te-conto/Transcendence).
> Regras transversais em `AGENTS.md` na raiz. Este serviço é **legado**: seu substituto
> é o serviço `lobby` em Go (onda 2 da migração).

## Responsabilidade

- Salas: criar, listar com paginação e filtro, entrar, sair, remover jogador, fechar.
- Jogadores: identidade (UUID), apelido, cor, posição no chaveamento, presença.
- Torneio: montar a árvore de partidas, travar inscrição, avançar rodada, promover
  vencedor.
- Pedir a criação de partida ao `Game-Core` e reagir ao resultado.
- WebSocket de sala: lista de jogadores, aviso de partida pronta, placar.

Não simula nada. Não conhece bola, paddle nem física.

## Stack

Django 5.1 + Channels 4.1 + uvicorn (ASGI), Postgres, Redis. Python 3.11.
`django-cors-headers` com `CORS_ALLOW_ALL_ORIGINS = True`. Sem DRF.

## Estrutura

```
src/
├── session/                 projeto Django
│   ├── settings.py          SECRET_KEY fixo no código
│   ├── asgi.py              http + websocket (AuthMiddlewareStack, não usado de fato)
│   ├── urls.py              /api/v1/user-session/{rooms,players,games}/
│   ├── routing.py           ws: .../ws/rooms/<code>/ e .../ws/match/<code>/
│   ├── consumers.py         RoomConsumer, PlayerScoreConsumer
│   └── repository.py        SessionRepository (acesso async)
├── rooms/                   salas e partidas
│   ├── models.py            Room (12 estados), Match (chaveamento)
│   ├── views.py             ★ 451 linhas, 7 views
│   ├── utils.py             ★ validação + createTournamentMatches (chaveamento)
│   └── tests.py             449 linhas, ~350 comentadas
├── players/
│   ├── models.py            Player, MatchPlayer
│   └── views.py             consulta e placar
├── games/
│   └── views.py             ★ GameView, TournamentGameView → publicam create_game
└── worker/
    ├── management/commands/game_integration.py    entrypoint
    └── listeners/orchestrator_listerner.py        ★ consome game-sync-session-queue
```

## Os arquivos que importam

**`rooms/utils.py:createTournamentMatches`** monta a árvore completa do torneio:
`ceil(log2(n))` rodadas, partidas por rodada, ligação `nextMatch`, e sorteio de
`bracketsPosition`. É a única função com teste que passa. Porta quase literalmente
para domínio Go.

**`games/views.py`** é a fronteira com o jogo: valida dono, lotação e tipo de sala, e
publica `create_game` na lista Redis.

**`worker/listeners/orchestrator_listerner.py`** é o outro lado: consome
`game-sync-session-queue`, marca a partida, promove o vencedor para a `nextMatch`,
recalcula `bracketsPosition` e reatribui cor, e avisa a sala quando a próxima partida
tem dois jogadores.

## Modelo de dados

```
Room (id, code[8], name, type, status, maxAmountOfPlayers, amountOfPlayers,
      privateRoom, stage, createdBy)
 ├── Player (id, name, roomCode, profileColor, score, isConnected, bracketsPosition)
 └── Match  (id, gameId, stage, position, nextMatch, winner, status)
      └── MatchPlayer (match, player, position)   ← unique_together
```

`Room.type`: 0 `MATCH`, 1 `TOURNAMENT`, 2 `SINGLE_PLAYER`.
`Room.status`: 12 valores em dois intervalos (0–7 para sala, 10–13 para torneio).
`Match.gameId` aponta para o `GameModel` no **outro** banco, sem integridade referencial.

## Como roda

| Container | Comando | Papel |
| --- | --- | --- |
| `user-session` | `uvicorn session.asgi:application --port 8002` | HTTP + WebSocket |
| `game-sync-session-worker` | `python manage.py game_integration` | orquestrador |
| `user-session-migrate` | `python manage.py migrate` | one-shot |

Sobe pelo compose da raiz (`make`).

## API

```
GET    /api/v1/user-session/rooms/?currentPage=&pageSize=&filterLabel=
POST   /api/v1/user-session/rooms/new-room/
GET    /api/v1/user-session/rooms/{code}/detail/
DELETE /api/v1/user-session/rooms/{code}/delete
GET    /api/v1/user-session/rooms/{code}/tournament/
GET    /api/v1/user-session/rooms/{code}/status/
PUT    /api/v1/user-session/rooms/{code}/add-player/
DELETE /api/v1/user-session/rooms/{code}/{playerId}/remove-player/
POST   /api/v1/user-session/rooms/{code}/lock-tournament/
POST   /api/v1/user-session/games/{code}/new-game/
POST   /api/v1/user-session/games/{code}/new-tournament-game/
GET    /api/v1/user-session/players/{id}/
GET    /api/v1/user-session/players/game/{gameId}/
POST   /api/v1/user-session/players/{code}/{color}/score/
WS     /api/v1/user-session/ws/rooms/{code}/?userId=
WS     /api/v1/user-session/ws/match/{code}/
```

Identidade vem do header `X-User-Id`. `POST /rooms/new-room/` e
`PUT /add-player/` **devolvem** o `X-User-Id` recém-criado no header da resposta.

## Integração

**Publica** — lista Redis `create-game-queue` com `{type:"create_game", roomId,
roomType, matchId, isSinglePlayer, stage, ownerId, players[{id,name,color}]}`.

**Consome** — lista Redis `game-sync-session-queue` (`LPOP` a cada 1 s).

**Channel layer Redis** — grupos `room_{roomCode}` (lista de jogadores, `sync.match`,
`delete_room`, `tournament_ended`), `room_{roomCode}_{matchId}` (`game.started`) e
`match_{roomCode}` (placar).

## Armadilhas deste serviço

- **`RoomConsumer.disconnect` não funciona.** `self.repository.update_player_connected_status(...)`
  é chamado **sem `await`** (a corrotina nunca executa) e ainda passa `True` em vez de
  `False`. Jogador fica marcado como conectado para sempre — e isso alimenta a espera
  de `send_message_game_start` no Game-Core.
- **`NameError` mascarando erro.** Em `orchestrator_listerner.process_game_sync`, o
  ramo `match is None` interpola `{e}`, que não está ligado nesse escopo.
- **Polling de 1 s** no orquestrador: cada avanço de rodada de torneio paga essa espera.
- **Sem idempotência.** Nenhum evento tem id; reprocessar `game-over` pontua de novo e
  reavança o chaveamento.
- **Sem autorização real.** `X-User-Id` é aceito como verdade.
  `UpdatePlayerScoreView` incrementa placar sem verificar nada.
- **Mapa de cor divergente** do Game-Core e do front-end (aqui: 0 RED, 1 BLUE, 2 GREEN,
  3 YELLOW).
- **`redis.Redis(host='redis')` fixo** em `games/views.py`.
- **Testes:** `games/tests.py` e `players/tests.py` são o stub do Django. Em
  `rooms/tests.py`, `RoomStatusViewTest` cria `Room(status='2')` — string num
  `IntegerField`.
- **`Room.status`** guarda às vezes o enum `RoomStatus`, às vezes o inteiro; `save()`
  faz a conversão, o que esconde o problema em vez de resolvê-lo.

## Para onde vai

Serviço `lobby` em Go, um único processo (o orquestrador vira consumidor JetStream no
mesmo binário):

| Aqui | Vai para |
| --- | --- |
| `rooms/models.py`, `rooms/utils.py` | `internal/domain/{room,tournament}.go` — regra pura, testada |
| `rooms/views.py`, `players/views.py` | `internal/adapter/http/` |
| `games/views.py` | caso de uso que publica `lobby.match.requested` |
| `worker/listeners/` | consumidor durável de `game.game.*`, idempotente por `eventId` |
| `session/consumers.py` | WS de sala, atrás do gateway |
| `Room.status` (12 inteiros) | `RoomState` como string, 5 valores; progresso de torneio sai para `Tournament.round` |

Correções feitas na travessia, de propósito: identidade por JWT, autorização de dono e
membro, `isConnected` funcionando, cor derivada do slot.
Ver [contratos](../docs/migration/03-contratos.md) e a skill `port-endpoint`.
