-- Cohors addon harness — exécute l'addon hors du jeu (lua5.1 + stubs des API Blizzard).
-- Usage : lua5.1 tools/addon-harness.lua                     (scénario « full »)
--         COHORS_SCENARIO=calendar lua5.1 tools/addon-harness.lua
--         COHORS_SCENARIO=apifail  lua5.1 tools/addon-harness.lua
--         COHORS_SCENARIO=slash    lua5.1 tools/addon-harness.lua
--         COHORS_SCENARIO=legacy   lua5.1 tools/addon-harness.lua   (GetAllRecipeIDs absente)
--         COHORS_SCENARIO=noapi    lua5.1 tools/addon-harness.lua   (aucune API recettes)
--         COHORS_SCENARIO=closemid lua5.1 tools/addon-harness.lua   (fenêtre fermée en cours de lecture)
--         COHORS_SCENARIO=noprofs  lua5.1 tools/addon-harness.lua
-- Simule fidèlement le sandbox du client : `os` et `io` sont retirés avant de charger
-- l'addon (le harnais capture ce dont il a besoin AVANT). Le temps est virtuel (GetTime).
-- Le JSON exporté est écrit dans /tmp/cohors-harness-out.json (validation côté python).

local getenv  = os.getenv        -- capturé avant la suppression (comme le client WoW)
local exitfn  = os.exit
local real_time, real_date = os.time, os.date
local io_open = io.open

local scenario = (getenv and getenv("COHORS_SCENARIO")) or "full"
local failures = 0

local function check(cond, fmt, ...)
    if cond then
        print("[ok]  " .. string.format(fmt, ...))
    else
        failures = failures + 1
        print("[ÉCHEC] " .. string.format(fmt, ...))
    end
end

-- --------------------------------------------------------------- stubs UI/frames
local M = { t = 100000.0 }              -- temps virtuel (GetTime)
local barValues = {}                    -- valeurs prises par la barre de progression
local frames = {}

local function newRegion(kind)
    local r = { _kind = kind, _scripts = {}, _shown = true, _value = 0 }
    local noop = function() end
    for _, m in ipairs({ "SetPoint", "SetSize", "SetFrameStrata", "SetMovable", "EnableMouse",
        "RegisterForDrag", "SetClampedToScreen", "SetAutoFocus", "SetTextInsets", "SetFontObject",
        "SetMultiLine", "SetStatusBarTexture", "SetMinMaxValues", "SetBackdrop", "SetJustifyH",
        "SetWordWrap", "SetAlpha", "SetScale", "RegisterEvent", "UnregisterEvent",
        "StartMoving", "StopMovingOrSizing", "HighlightText", "SetFocus", "ClearFocus" }) do
        r[m] = noop
    end
    r.SetScript = function(self, ev, fn) self._scripts[ev] = fn end
    r.GetScript = function(self, ev) return self._scripts[ev] end
    r.Show = function(self) self._shown = true end
    r.Hide = function(self) self._shown = false end
    r.IsShown = function(self) return self._shown end
    r.SetText = function(self, t) self._text = tostring(t or "") end
    r.GetText = function(self) return self._text or "" end
    r.SetWidth = function(self, w) self._w = w end
    r.SetStatusBarColor = noop
    r.SetValue = function(self, v)
        self._value = tonumber(v) or 0
        if kind == "StatusBar" then barValues[#barValues + 1] = self._value end
    end
    r.GetValue = function(self) return self._value end
    r.CreateFontString = function() return newRegion("fontstring") end
    r.CreateTexture = function() return newRegion("texture") end
    return r
end

CreateFrame = function(ftype, name, parent, template)
    local f = newRegion(ftype or "frame")
    f._type = ftype
    f._name = name
    frames[#frames + 1] = f
    return f
end
UIParent = newRegion("frame")
local CHAT = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(_, msg) CHAT[#CHAT + 1] = tostring(msg); print("[chat] " .. tostring(msg)) end }
local function chat_has(sub)
    for _, m in ipairs(CHAT) do
        if m:find(sub, 1, true) then return true end
    end
    return false
end
local GameFontNormal, GameFontNormalSmall, GameFontHighlightSmall, ChatFontNormal = {}, {}, {}, {}

-- -------------------------------------------------------------- API Blizzard
time = real_time
date = real_date
GetTime = function() return M.t end
GetBuildInfo = function() return "12.1.0", "69814", "Sep 1 2026", 120100, "12.1.0" end
GetRealmName = function() return "Hyjal" end
UnitName = function() return "Chamoisdort" end
GetNumAddOns = function() return 1 end
GetAddOnInfo = function() return "Cohors", "Cohors", "", true, "LOADED" end
GetAddOnMetadata = function(_, f) return f == "Version" and "1.7.0" or nil end
C_AddOns = { GetNumAddOns = GetNumAddOns, GetAddOnInfo = GetAddOnInfo, GetAddOnMetadata = GetAddOnMetadata }
C_Item = { GetItemNameByID = function(id) return "Composant " .. tostring(id) end,
           GetItemInfo = function(id) return "Composant " .. tostring(id) end }
C_Timer = { After = function() end }
SlashCmdList = {}
ToggleCalendar = function() end

local tnow = real_time()
local dtom = real_date("*t", tnow + 86400)
C_DateAndTime = {
    GetCurrentCalendarTime = function()
        local d = real_date("*t", real_time())
        return { year = d.year, month = d.month, monthDay = d.day, hour = d.hour, minute = d.min }
    end,
    AdjustTimeByDays = function(ct) return ct end,
    GetCalendarTimeFromEpoch = function()
        local d = real_date("*t", real_time() + 21 * 86400)
        return { year = d.year, month = d.month, monthDay = d.day, hour = 12, minute = 0 }
    end,
}
C_Club = { GetSubscribedClubs = function() return {} end, GetGuildClubId = function() return 1234 end }

local calOpened = false
local calScan = 0          -- mois actuellement « affiché » par le mock
local pendingEvent = false -- un événement vient d'être ouvert : le client répondra une fois
C_Calendar = {
    OpenCalendar = function() end,
    SetAbsMonth = function() calScan = 0 end,
    SetMonth = function(s) calScan = tonumber(s) or 0 end,
    GetMonthInfo = function()
        local d = real_date("*t", real_time())
        return { month = d.month, year = d.year, numDays = 30 }
    end,
    GetNumDayEvents = function(off, day)
        if scenario == "calendar" and calScan == 0 and day == 5 then return 1 end
        return 0
    end,
    GetDayEvent = function(off, day, i)
        if scenario == "calendar" and calScan == 0 and day == 5 and i == 1 then
            return { eventID = 77, title = "Raid test", calendarType = "GUILD_EVENT", eventType = 0,
                     startTime = { year = dtom.year, month = dtom.month, monthDay = dtom.day, hour = 20, minute = 30 } }
        end
        return nil
    end,
    OpenEvent = function(off, day, idx) calOpened = true; pendingEvent = true; return true end,
    CloseEvent = function() calOpened = false end,
    GetNumInvites = function() return calOpened and 1 or 0 end,
    EventGetInvite = function(j)
        if calOpened and j == 1 then return { name = "Tester", inviteStatus = 1 } end
        return nil
    end,
    GetNumGuildEvents = function() return 0 end,
}

GetProfessions = function()
    if scenario == "noprofs" then return nil end
    return 1, 2
end
GetProfessionInfo = function(idx)
    if idx == 1 then return "Alchimie", "icon", 100, 100, 0, 0, 171, 171 end
    return "Cuisine", "icon", 100, 100, 0, 0, 185, 185
end

local profData = {
    [171] = { childs = { { professionID = 1001, parentProfessionID = 171, expansionName = "Khaz Algar", recipeIDs = { 11, 12, 13 } },
                         { professionID = 1002, parentProfessionID = 171, expansionName = "Dragon Isles", recipeIDs = { 14, 15, 16 },
                           unlearned = { 16 } } } },
    [185] = { childs = { { professionID = 2001, parentProfessionID = 185, expansionName = "Khaz Algar", recipeIDs = { 21, 22 } } } },
}
if scenario == "apifail" then
    profData = { [171] = {}, [185] = {} }     -- ni prêt, ni liste : tout doit échouer proprement
end

local TS = { open = nil, child = nil, pendingShow = false }
local function currentChilds()
    local p = profData[TS.open]
    return p and p.childs or nil
end
local function currentChild()
    local cs = currentChilds()
    if not cs then return nil end
    for _, c in ipairs(cs) do
        if c.professionID == TS.child then return c end
    end
    return nil
end

C_TradeSkillUI = {
    OpenTradeSkill = function(skillLine)
        -- comme en jeu : le client ne l'accepte QUE depuis un événement matériel, sinon il ne se
        -- passe rien (pas d'erreur). Le scénario « apifail » simule un client qui refuse toujours.
        if scenario == "apifail" then return false end
        TS.open = skillLine; TS.child = nil; TS.pendingShow = true
        return true
    end,
    IsTradeSkillReady = function() return scenario ~= "apifail" end,
    GetChildProfessionInfos = function() return currentChilds() end,
    SetProfessionChildSkillLineID = function(id) TS.child = id end,
    GetChildProfessionInfo = function()
        local c = currentChild()
        return c and { professionID = c.professionID, parentProfessionID = c.parentProfessionID,
                       expansionName = c.expansionName } or nil
    end,
    GetAllRecipeIDs = function()
        if scenario == "legacy" or scenario == "noapi" then error("api indisponible (test)") end
        local c = currentChild()
        return c and c.recipeIDs or {}
    end,
    GetFilteredRecipeIDs = function()
        -- API supprimée du client actuel : elle n'existe QUE dans le scénario « legacy ».
        if scenario ~= "legacy" then error("api supprimée du client (test)") end
        local c = currentChild()
        return c and c.recipeIDs or {}
    end,
    GetRecipeInfo = function(rid)
        local c = currentChild()
        local learned = true
        if c and c.unlearned then
            for _, u in ipairs(c.unlearned) do
                if u == rid then learned = false end
            end
        end
        return { name = "Recette " .. tostring(rid), recipeID = rid, learned = learned }
    end,
    GetRecipeSchematic = function(rid)
        return { reagentSlotSchematics = { { quantityRequired = 2, reagents = { { itemID = 9000 + rid } } } } }
    end,
    CloseTradeSkill = function() TS.open = nil; TS.child = nil end,
}

-- ------------------------------------------------------- chargement de l'addon
os = nil
io = nil

local chunk, lerr = loadfile("addon/Cohors/Cohors.lua")
if not chunk then
    print("[ÉCHEC] loadfile : " .. tostring(lerr))
    exitfn(1)
end
local ok, err = pcall(chunk, "Cohors")
check(ok, "chargement de l'addon sans os/io (%s)", ok and "sans erreur" or tostring(err))
if not ok then exitfn(1) end

local driver
for _, fr in ipairs(frames) do
    if fr._scripts["OnEvent"] then driver = fr end
end
check(driver ~= nil, "moteur OnUpdate/OnEvent enregistré")

if driver then
    local ok2, err2 = pcall(driver._scripts["OnEvent"], driver, "ADDON_LOADED", "Cohors")
    check(ok2, "événement ADDON_LOADED (%s)", ok2 and "sans erreur" or tostring(err2))
end

local function tick(n)
    for _ = 1, (n or 1) do
        M.t = M.t + 0.25
        if driver and driver._scripts["OnUpdate"] then
            local okX, errX = pcall(driver._scripts["OnUpdate"], driver)
            if not okX then
                check(false, "OnUpdate moteur : %s", tostring(errX))
                return false
            end
        end
        if scenario == "calendar" and pendingEvent and driver and driver._scripts["OnEvent"] then
            pendingEvent = false
            pcall(driver._scripts["OnEvent"], driver, "CALENDAR_OPEN_EVENT")   -- le calendrier répond
        end
        if TS.pendingShow and driver and driver._scripts["OnEvent"] then
            TS.pendingShow = false
            pcall(driver._scripts["OnEvent"], driver, "TRADE_SKILL_SHOW")   -- le joueur ouvre la fenêtre
        end
        for _, fr in ipairs(frames) do
            local h = fr._scripts["OnUpdate"]
            if h and fr ~= driver and fr:IsShown() then
                local okY, errY = pcall(h, fr)
                if not okY then
                    check(false, "OnUpdate fenêtre : %s", tostring(errY))
                    return false
                end
            end
        end
    end
    return true
end

local function progressFrame()
    for _, fr in ipairs(frames) do
        if fr._name == "CohorsProgressFrame" then return fr end
    end
end

-- ------------------------------------------------------------- scénario demandé
if scenario == "calendar" then
    barValues = {}
    Cohors_Collect()
    -- exclusion : l'export des recettes doit être refusé pendant la collecte
    Cohors_Recipes()
    check(chat_has("attends la fin (ou /cohors reset)"), "recettes refusées pendant la collecte calendrier")
    check(Cohors_DB.recipes_at == nil, "aucun export recettes lancé pendant la collecte")
    local done = false
    for _ = 1, 4000 do
        if not tick(1) then break end
        if Cohors_DB.export then done = true; break end
    end
    check(done, "collecte calendrier terminée en %.1f s virtuelles", M.t - 100000.0)
    local ex = Cohors_DB.export or ""
    check(ex:find("Raid test", 1, true) ~= nil, "événement « Raid test » dans l'export")
    check(ex:find("Tester", 1, true) ~= nil, "invité « Tester » dans l'export")
    local increasing = true
    for i = 2, #barValues do
        if barValues[i] < barValues[i - 1] then increasing = false end
    end
    check(#barValues >= 5 and barValues[#barValues] == 100 and increasing,
        "barre de progression : %d maj, finale %s, monotone=%s",
        #barValues, tostring(barValues[#barValues]), tostring(increasing))
    tick(45)
    local pf = progressFrame()
    check(pf ~= nil and not pf:IsShown(), "fenêtre de progression refermée après la fin")

elseif scenario == "noprofs" then
    Cohors_Recipes()
    tick(40)
    check(Cohors_DB.recipes_at == nil, "aucun métier : rien exporté, pas de plantage")

elseif scenario == "closemid" then
    -- la fenêtre est fermée en cours de lecture : clôture propre, les autres métiers continuent
    barValues = {}
    Cohors_Recipes()
    local closed, done = false, false
    for _ = 1, 4000 do
        if not tick(1) then break end
        if not closed and (M.t - 100000.0) > 3.5 then
            closed = true
            TS.open = nil; TS.child = nil            -- le joueur ferme la fenêtre
        end
        if Cohors_DB.recipes_at then done = true; break end
    end
    check(done, "export terminé malgré la fermeture (%.1f s virtuelles)", M.t - 100000.0)
    check(chat_has("fenêtre fermée"), "fermeture détectée et signalée")
    check((Cohors_DB.recipes_total or 0) >= 2, "métiers restants lus après la fermeture (total %s)",
        tostring(Cohors_DB.recipes_total))

elseif scenario == "slash" then
    -- v1.7.1 : « /cohors » sans argument ouvre le panneau, ne collecte RIEN tout seul.
    SlashCmdList["Cohors"]("")
    tick(30)
    check(not chat_has("lecture du calendrier"), "« /cohors » n'auto-collecte plus le calendrier")
    check(Cohors_DB.export == nil, "aucune collecte lancée par « /cohors »")

else
    -- full / apifail : export des recettes
    barValues = {}
    Cohors_Recipes()
    -- exclusion : la collecte calendrier doit être refusée pendant l'export recettes
    Cohors_Collect()
    check(chat_has("attends la fin avant de lancer le calendrier"), "calendrier refusé pendant l'export recettes")
    check(Cohors_DB.export == nil, "aucune collecte calendrier lancée pendant l'export")
    local done = false
    for _ = 1, 4000 do
        if not tick(1) then break end
        if Cohors_DB.recipes_at then done = true; break end
    end
    check(done, "export des recettes terminé (%.1f s virtuelles)", M.t - 100000.0)
    check(Cohors_DB.recipes ~= nil and #Cohors_DB.recipes > 20, "chaîne JSON exportée (%d caractères)",
        Cohors_DB.recipes and #Cohors_DB.recipes or 0)
    check(#barValues >= 5, "fenêtre de progression alimentée (%d maj de barre)", #barValues)
    local increasing = true
    for i = 2, #barValues do
        if barValues[i] < barValues[i - 1] then increasing = false end
    end
    check(increasing, "barre monotone croissante")
    check(barValues[#barValues] == 100, "barre à 100 %% à la fin (dernière valeur %s)",
        tostring(barValues[#barValues]))
    if scenario == "apifail" then
        check(Cohors_DB.recipes_total == 0, "client qui refuse d'ouvrir : 0 recette (total %s)",
            tostring(Cohors_DB.recipes_total))
        check(Cohors_DB.recipes_at ~= nil, "export vide mais valide écrit malgré le refus")
        check(chat_has("délai dépassé"), "délai d'inactivité signalé dans le chat")
    elseif scenario == "legacy" then
        check(Cohors_DB.recipes_total == 7, "repli GetFilteredRecipeIDs (vieux client) : 7 recettes (total %s)",
            tostring(Cohors_DB.recipes_total))
    elseif scenario == "noapi" then
        check(Cohors_DB.recipes_total == 0, "aucune API : 0 recette proprement (total %s)",
            tostring(Cohors_DB.recipes_total))
        check(chat_has("API recettes en échec"), "avertissement API affiché dans le chat")
    else
        check(Cohors_DB.recipes_total == 7, "7 recettes attendues (2 métiers, 3 paliers) — trouvées %s",
            tostring(Cohors_DB.recipes_total))
        check((Cohors_DB.rec_diag or ""):find("1 non apprises", 1, true) ~= nil,
            "recette non apprise filtrée (rapport rec_diag)")
    end
    tick(45)
    local pf = progressFrame()
    check(pf ~= nil and not pf:IsShown(), "fenêtre de progression refermée après la fin")
    local outf = io_open and io_open("/tmp/cohors-harness-out.json", "w")
    if outf then
        outf:write(Cohors_DB.recipes or "")
        outf:close()
    end
end

print("")
if failures == 0 then
    print("HARNAIS OK — scénario " .. scenario)
    exitfn(0)
else
    print("HARNAIS EN ÉCHEC — " .. failures .. " problème(s), scénario " .. scenario)
    exitfn(1)
end
