-- S.1 spike: turn-event logger and hold test. Run over the tuner, once per Lua context.
--
-- Test code only. It isn't an installed mod and leaves nothing on disk. It's sent as raw Lua, with
-- no escaping, so it must never be exposed to a model. scripts/spike_hold_turn.py fills in
-- __CONTEXT__ and __TARGET__ after checking that the target is a plain integer.

local CONTEXT = "__CONTEXT__"
local TARGET = __TARGET__
local MAX_LOG_ENTRIES = 20000
local ANY_PLAYER = -1

if not ExposedMembers.Civ6AiS1 then
  ExposedMembers.Civ6AiS1 = { log = {}, seq = 0, cleanups = {}, hold = { state = "off" } }
end
local S1 = ExposedMembers.Civ6AiS1

local function now()
  return Automation.GetTime()
end

local function safe(read)
  local ok, value = pcall(read)
  return ok and tostring(value) or "ERR"
end

local function digest(id)
  local player = Players[id]
  local count, moves, hash = 0, 0, 7
  local ok = pcall(function()
    for _, unit in player:GetUnits():Members() do
      count = count + 1
      moves = moves + unit:GetMovesRemaining()
      hash = (hash * 31 + unit:GetX() * 1009 + unit:GetY() * 13 + unit:GetID()) % 1000000007
    end
  end)
  if not ok then
    return "ERR"
  end
  local gold = safe(function() return player:GetTreasury():GetGoldBalance() end)
  local tech = safe(function() return player:GetTechs():GetResearchingTech() end)
  local cities = safe(function() return player:GetCities():GetCount() end)
  return table.concat({ count, moves, hash, gold, tech, cities }, "/")
end

local function active_majors()
  local active = {}
  for _, id in ipairs(PlayerManager.GetAliveMajorIDs()) do
    if Players[id]:IsTurnActive() then
      active[#active + 1] = id
    end
  end
  return table.concat(active, ",")
end

local function record(event, args, with_digest)
  if S1.seq >= MAX_LOG_ENTRIES then
    return
  end
  S1.seq = S1.seq + 1
  local fields = {
    S1.seq,
    string.format("%.3f", now()),
    CONTEXT,
    event,
    args,
    safe(function() return Game.GetCurrentGameTurn() end),
    with_digest and safe(active_majors) or "",
    with_digest and digest(TARGET) or "",
  }
  S1.log[S1.seq] = table.concat(fields, "|")
end

local function spin(hold)
  hold.state = "held"
  hold.started = now()
  record("HOLD_START", hold.kind, true)
  while not hold.release and now() - hold.started < hold.max_seconds do
  end
  hold.state = hold.release and "released" or "timed_out"
  record("HOLD_END", hold.state, true)
end

local function begin_hold(hold)
  if hold.kind == "spin" then
    spin(hold)
  elseif hold.kind == "ui_event" then
    hold.event_id = UI.ReferenceCurrentEvent()
    hold.state = "held"
    hold.started = now()
    record("HOLD_START", "ui_event " .. tostring(hold.event_id), true)
  elseif hold.kind == "pause" then
    local config = PlayerConfigurations[Game.GetLocalPlayer()]
    config:SetWantsPause(true)
    Network.BroadcastPlayerInfo()
    hold.state = "held"
    hold.started = now()
    record("HOLD_START", "pause " .. tostring(GameConfiguration.IsPaused()), true)
  end
end

local function maybe_hold(event, player)
  local hold = S1.hold
  if hold.state == "armed" and hold.context == CONTEXT and hold.event == event
    and (hold.player == player or hold.player == ANY_PLAYER) then
    begin_hold(hold)
  end
end

local function attach(event_table, table_name, name, with_digest)
  local handler = function(...)
    local raw = { ... }
    local args = {}
    for index = 1, #raw do
      args[index] = tostring(raw[index])
    end
    record(table_name .. "." .. name, table.concat(args, ","), with_digest)
    maybe_hold(name, raw[1])
  end
  local ok = pcall(function()
    event_table[name].Add(handler)
  end)
  if ok then
    S1.cleanups[#S1.cleanups + 1] = function()
      event_table[name].Remove(handler)
    end
  end
  print("HOOK|" .. CONTEXT .. "|" .. table_name .. "." .. name .. "|" .. tostring(ok))
end

for _, name in ipairs({
  "PlayerTurnStarted", "PlayerTurnStartComplete", "PlayerTurnDeactivated", "OnPlayerTurnEnded",
  "OnGameTurnStarted", "OnUnitMoved", "CityBuilt", "UnitCreated",
}) do
  attach(GameEvents, "GameEvents", name, name ~= "OnUnitMoved")
end
for _, name in ipairs({
  "PreTurnBegin", "TurnBegin", "TurnEnd", "PlayerTurnActivated", "PlayerTurnDeactivated",
  "LocalPlayerTurnBegin", "LocalPlayerTurnEnd", "RemotePlayerTurnBegin", "RemotePlayerTurnEnd",
  "GamePauseStateChanged",
}) do
  attach(Events, "Events", name, true)
end
for _, name in ipairs({ "UnitMoved", "UnitMoveComplete", "UnitOperationStarted" }) do
  attach(Events, "Events", name, false)
end
print("---END---")
