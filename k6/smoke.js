// Teste de fumaça e carga leve do User-Session, contra uma instância local
// (subida pelo próprio job de CI, com Postgres e Redis reais — sem mock).
//
// Cobre o ciclo de vida de uma sala 1v1 na ordem que um jogador de verdade
// percorre, com o contrato REAL do serviço (validado ao vivo no cluster k3s
// em 2026-09-12, não presumido pela leitura do código):
//   - players indexado por posição de cor, não array
//   - campo é `color`, não `profileColor`, na resposta de detalhe
//   - dono é sinalizado por `owner` (por jogador) e `data.owner` (viewer)
//   - remove-player identifica o alvo por cor (int), não por id (uuid)
//   - X-User-Color, devolvido no create/join, é o que habilita "sair da sala"
//
// Rodar localmente: k6 run -e BASE_URL=http://localhost:8002 k6/smoke.js
import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8002";
const API = `${BASE_URL}/api/v1/user-session`;

// Latências por rota, separadas — uma média única esconde a rota lenta.
const createRoomDuration = new Trend("create_room_duration", true);
const listRoomsDuration = new Trend("list_rooms_duration", true);
const roomDetailDuration = new Trend("room_detail_duration", true);

export const options = {
  scenarios: {
    // Um VU percorrendo o fluxo completo repetidas vezes: prova que o ciclo
    // de vida inteiro funciona sob alguma repetição, não só uma vez.
    room_lifecycle: {
      executor: "constant-vus",
      vus: 3,
      duration: "20s",
      exec: "roomLifecycle",
    },
    // Leitura pura da lista de salas, em paralelo — é a rota mais visitada
    // (home / lista de salas), e a única com paginação a validar sob carga.
    list_rooms: {
      executor: "constant-vus",
      vus: 5,
      duration: "20s",
      exec: "listRooms",
      startTime: "2s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    create_room_duration: ["p(95)<800"],
    list_rooms_duration: ["p(95)<400"],
    room_detail_duration: ["p(95)<400"],
    // Nenhuma rota deve estourar 2s no p99 — acima disso o jogador percebe.
    http_req_duration: ["p(99)<2000"],
  },
};

function jsonHeaders(extra) {
  return { headers: Object.assign({ "Content-Type": "application/json" }, extra || {}) };
}

export function roomLifecycle() {
  const roomName = `k6-${__VU}-${__ITER}`;

  // 1. cria a sala — devolve X-User-Id e X-User-Color no header, não no corpo.
  const createRes = http.post(
    `${API}/rooms/new-room/`,
    JSON.stringify({
      createdBy: `k6-owner-${__VU}`,
      roomType: 0, // MATCH — só 2 jogadores (decisão D3, 4p cortado)
      maxAmountOfPlayers: 2,
      roomName,
      privateRoom: false,
    }),
    jsonHeaders()
  );
  createRoomDuration.add(createRes.timings.duration);

  const created = check(createRes, {
    "criar sala: 201": (r) => r.status === 201,
    "criar sala: devolve X-User-Id": (r) => !!r.headers["X-User-Id"],
    "criar sala: devolve X-User-Color": (r) => r.headers["X-User-Color"] !== undefined,
    "criar sala: roomCode no corpo": (r) => !!r.json("roomCode"),
  });
  if (!created) {
    // Sala não foi criada — não adianta seguir o fluxo com dado inexistente.
    return;
  }

  const roomCode = createRes.json("roomCode");
  const ownerId = createRes.headers["X-User-Id"];
  const ownerColor = createRes.headers["X-User-Color"];

  // 2. um segundo jogador entra.
  const joinRes = http.put(
    `${API}/rooms/${roomCode}/add-player/`,
    JSON.stringify({ name: `k6-joiner-${__VU}`, roomCode }),
    jsonHeaders()
  );
  check(joinRes, {
    "entrar na sala: 201": (r) => r.status === 201,
    "entrar na sala: devolve X-User-Color": (r) => r.headers["X-User-Color"] !== undefined,
  });

  // 3. o dono vê o detalhe da sala — contrato real: players é objeto por cor.
  const detailRes = http.get(`${API}/rooms/${roomCode}/detail/`, jsonHeaders({ "X-User-Id": ownerId }));
  roomDetailDuration.add(detailRes.timings.duration);
  check(detailRes, {
    "detalhe da sala: 200": (r) => r.status === 200,
    "detalhe da sala: owner=true para o dono": (r) => r.json("owner") === true,
    "detalhe da sala: 2 jogadores": (r) => Object.keys(r.json("players")).length === 2,
    "detalhe da sala: cada jogador tem campo color": (r) =>
      Object.values(r.json("players")).every((p) => typeof p.color === "number"),
  });

  // 4. status da sala — endpoint simples, mas usado em polling pelo front.
  const statusRes = http.get(`${API}/rooms/${roomCode}/status/`);
  check(statusRes, { "status da sala: 200": (r) => r.status === 200 });

  sleep(0.1);

  // 5. o dono sai — remove-player por COR, não por id (contrato real).
  const leaveRes = http.del(
    `${API}/rooms/${roomCode}/${ownerColor}/remove-player/`,
    null,
    jsonHeaders({ "X-User-Id": ownerId })
  );
  check(leaveRes, {
    "sair da sala: 204": (r) => r.status === 204,
  });
}

export function listRooms() {
  const res = http.get(`${API}/rooms/?currentPage=1&pageSize=10&filterLabel=`);
  listRoomsDuration.add(res.timings.duration);
  check(res, {
    "listar salas: 200": (r) => r.status === 200,
    // Nível raiz, sem envelope paginatedItems (contrato real pós-rebase).
    "listar salas: tem content e currentPage no nível raiz": (r) =>
      Array.isArray(r.json("content")) && typeof r.json("currentPage") === "number",
  });
}
