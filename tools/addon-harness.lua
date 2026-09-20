-- Cohors addon harness — exécute l'addon hors du jeu (lua5.1 + stubs des API Blizzard).
-- Usage : lua5.1 tools/addon-harness.lua                     (scénario « full »)
--         COHORS_SCENARIO=calendar lua5.1 tools/addon-harness.lua
--         COHORS_SCENARIO=apifail  lua5.1 tools/addon-harness.lua
--         COHORS_SCENARIO=slash    lua5.1 tools/addon-harness.lua
--         COHORS_SCENARIO=legacy   lua5.1 tools/addon-harness.lua   (GetAllRecipeIDs absente)
--         COHORS_SCENARIO=noapi    lua5.1 tools/addon-harness.lua   (aucune API recettes)
--         COHORS_SCENARIO=closemid lua5.1 tools/addon-harness.lua   (fenêtre fermée en cours de lecture)
--         COHORS_SCENARIO=openbtn  lua5.1 tools/addon-harness.lua   (le client ne cède qu'au clic « Ouvrir »)
--         COHORS_SCENARIO=unreadable lua5.1 tools/addon-harness.lua (métier sans fenêtre standard, cas archéologie)
--         COHORS_SCENARIO=logout   lua5.1 tools/addon-harness.lua   (/reload en plein export : rien ne se perd)
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
local M = { t = 100000.0, hardware = false }  -- temps virtuel + « clic » simulé (événement matériel)
local barValues = {}                    -- valeurs prises par la barre de progression
local frames = {}

-- Événements VALIDES dans le client : une inscription d'un nom inconnu lève une erreur qui
-- INTERROMPT le chargement du fichier en jeu (vécu : TRADE_SKILL_UPDATE en v1.8.0 → addon à
-- moitié chargée + « attempt to call a nil value » en boucle). Le harnais doit être aussi strict
-- que le client, sinon ce type d'avarie passe la CI sans être vu.
local VALID_EVENTS = {
    ADDON_LOADED = true, CALENDAR_OPEN_EVENT = true,
    TRADE_SKILL_SHOW = true, TRADE_SKILL_CLOSE = true,
    PLAYER_ENTERING_WORLD = true, ZONE_CHANGED_NEW_AREA = true, PLAYER_LOGOUT = true,
}
local function newRegion(kind)
    local r = { _kind = kind, _scripts = {}, _shown = true, _value = 0, _enabled = true }
    local noop = function() end
    for _, m in ipairs({ "SetPoint", "SetSize", "SetFrameStrata", "SetMovable", "EnableMouse",
        "RegisterForDrag", "SetClampedToScreen", "SetAutoFocus", "SetTextInsets", "SetFontObject",
        "SetMultiLine", "SetStatusBarTexture", "SetMinMaxValues", "SetBackdrop", "SetJustifyH",
        "SetWordWrap", "SetAlpha", "SetScale", "SetHeight", "ClearAllPoints", "SetHighlightTexture",
        "SetScrollChild", "EnableMouseWheel", "SetTexture", "SetAllPoints", "SetColorTexture",
        "StartMoving", "StopMovingOrSizing", "HighlightText", "SetFocus", "ClearFocus" }) do
        r[m] = noop
    end
    r.RegisterEvent = function(self, ev)
        if not VALID_EVENTS[ev] then
            error("Attempt to register unknown event \"" .. tostring(ev) .. "\"")
        end
    end
    r.UnregisterEvent = function() end
    r.Enable = function(self) self._enabled = true end
    r.Disable = function(self) self._enabled = false end
    r.IsEnabled = function(self) return self._enabled ~= false end
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
    r.SetChecked = function(self, v) self._checked = v and true or false end
    r.GetChecked = function(self) return self._checked == true end
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
-- Wishlist (v1.12.0) : noms/liens d'objets configurables par scénario, sort des recettes
local C_ITEM_NAMES, C_ITEM_LINKS, C_ITEM_SPELLS = {}, {}, {}
C_Item = {
    GetItemNameByID = function(id) return C_ITEM_NAMES[id] or ("Composant " .. tostring(id)) end,
    GetItemInfo = function(id) return C_ITEM_NAMES[id] or ("Composant " .. tostring(id)), C_ITEM_LINKS[id] end,
    GetItemSpell = function(link)
        local sid = link and C_ITEM_SPELLS[link]
        if sid then return "Sort de recette", sid end
        return nil
    end,
}
-- C_Timer.After exécute la tâche immédiatement (l'addon n'en utilise qu'une : le scan d'instance)
C_Timer = { After = function(_, fn) if type(fn) == "function" then fn() end end }

-- Instance + Journal d'aventure simulés : WL est rempli par le scénario « wishlist »
local WL = { instance = nil, jid = nil, loot = {}, known = {}, extra = {} }
GetInstanceInfo = function()
    if not WL.instance then return nil end
    return WL.instance.name, WL.instance.itype, 8, WL.instance.diff or "Raid normal",
        WL.instance.max or 20, false, false, WL.instance.mapID, WL.instance.mapID
end
EJ_GetInstanceForMap = function(_mapID) return WL.jid end
EJ_SelectInstance = function(_jid) WL.cur = "inst" end
EJ_SelectEncounter = function(eid) WL.cur = eid end
EJ_GetEncounterInfoByIndex = function(i, _jid)
    local b = WL.loot[i]
    if not b then return nil end
    return 1000 + i, b.name
end
EJ_GetNumLoot = function()
    if WL.cur == "inst" then return #(WL.extra or {}) end
    local i = (type(WL.cur) == "number") and (WL.cur - 1000) or 0
    local b = WL.loot[i]
    return (b and #(b.items or {})) or 0
end
EJ_GetLootInfoByIndex = function(j)
    local list
    if WL.cur == "inst" then
        list = WL.extra or {}
    else
        local i = (type(WL.cur) == "number") and (WL.cur - 1000) or 0
        list = (WL.loot[i] and WL.loot[i].items) or {}
    end
    local id = list[j]
    if id then return { itemID = id } end
    return nil
end
C_EncounterJournal = { InstanceHasLoot = function(_jid) return WL.hasLoot ~= false end }
IsSpellKnown = function(sp) return WL.known[sp] == true end
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
        -- comme en jeu : le client ne l'accepte QUE depuis un événement matériel (clic → M.hardware),
        -- sinon il refuse en silence. « apifail » refuse toujours ; « openbtn » n'accepte QUE les
        -- clics (le harnais clique sur « ▶ Ouvrir » comme le ferait le joueur).
        if scenario == "apifail" then return false end
        if (scenario == "openbtn" or scenario == "unreadable") and not M.hardware then return false end
        if scenario == "unreadable" and skillLine == 185 then
            return true   -- « accepté » mais aucune fenêtre standard ne s'ouvre (cas archéologie)
        end
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
        -- En jeu, cette API renvoie TOUT le métier, tous paliers confondus (vérifié dans la
        -- trace de Fred : mêmes 96/116/194 ids quel que soit le palier affiché). Le mock
        -- concatène donc les paliers — et l'addon ne bascule plus de palier du tout.
        if scenario == "legacy" or scenario == "noapi" then error("api indisponible (test)") end
        local p = profData[TS.open]
        if not p then return {} end
        local out = {}
        for _, c in ipairs(p.childs or {}) do
            for _, rid in ipairs(c.recipeIDs or {}) do out[#out + 1] = rid end
        end
        return out
    end,
    GetFilteredRecipeIDs = function()
        -- API supprimée du client actuel : elle n'existe QUE dans le scénario « legacy ».
        if scenario ~= "legacy" then error("api supprimée du client (test)") end
        local p = profData[TS.open]
        if not p then return {} end
        local out = {}
        for _, c in ipairs(p.childs or {}) do
            for _, rid in ipairs(c.recipeIDs or {}) do out[#out + 1] = rid end
        end
        return out
    end,
    GetRecipeInfo = function(rid)
        local p = profData[TS.open]
        local learned = true
        for _, c in ipairs((p and p.childs) or {}) do
            for _, u in ipairs(c.unlearned or {}) do
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

local function openButton()
    for _, fr in ipairs(frames) do
        if fr._name == "CohorsProgressOpenBtn" then return fr end
    end
end

-- ------------------------------------------------------------- scénario demandé
if scenario == "calendar" then
    barValues = {}
    SlashCmdList["Cohors"]("")   -- panneau ouvert : le champ partagé doit se remplir tout seul
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
    local obc = openButton()
    check(obc ~= nil and obc:IsShown() == false,
        "bouton « Ouvrir » masqué pendant la collecte du calendrier (réservé aux recettes)")
    local ex = Cohors_DB.export or ""
    check(ex:find("Raid test", 1, true) ~= nil, "événement « Raid test » dans l'export")
    check(ex:find("Tester", 1, true) ~= nil, "invité « Tester » dans l'export")
    -- le texte à exporter apparaît dans le champ DÈS la fin de la collecte (plus de bouton « Exporter »)
    local calField
    for i = #frames, 1, -1 do
        if frames[i]._type == "EditBox" then calField = frames[i]; break end
    end
    check(calField ~= nil and (calField._text or ""):find('"v":1', 1, true) ~= nil,
        "chaîne affichée automatiquement dans le champ (sans clic « Exporter »)")
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
        if not closed and (M.t - 100000.0) > 0.5 then
            closed = true
            TS.open = nil; TS.child = nil            -- le joueur ferme la fenêtre
        end
        if Cohors_DB.recipes_total ~= nil then done = true; break end
    end
    check(done, "export terminé malgré la fermeture (%.1f s virtuelles)", M.t - 100000.0)
    check(chat_has("fenêtre fermée"), "fermeture détectée et signalée")
    check((Cohors_DB.recipes_total or 0) >= 2, "métiers restants lus après la fermeture (total %s)",
        tostring(Cohors_DB.recipes_total))

elseif scenario == "openbtn" then
    -- le client refuse toute ouverture hors clic : seul le bouton « Ouvrir » (cliqué ici, comme
    -- le joueur) fait avancer l'export — c'est le comportement réel en jeu.
    barValues = {}
    Cohors_Recipes()
    tick(4)
    local done = false
    for _ = 1, 4000 do
        if not tick(1) then break end
        if (M.t - 100000.0) % 4 < 0.26 then
            local ob = openButton()
            if ob and ob:IsEnabled() and ob._scripts["OnClick"] then
                M.hardware = true
                pcall(ob._scripts["OnClick"], ob, "LeftButton", false)   -- clic simulé
                M.hardware = false
            end
        end
        if Cohors_DB.recipes_total ~= nil then done = true; break end
    end
    check(done, "export terminé via les clics sur « ▶ Ouvrir » (%.1f s virtuelles)", M.t - 100000.0)
    check(Cohors_DB.recipes_total == 7, "7 recettes lues grâce aux clics (total %s)",
        tostring(Cohors_DB.recipes_total))
    check(((openButton() and openButton():GetText()) or ""):find("terminé", 1, true) ~= nil,
        "bouton passé à « ✔ terminé »")

elseif scenario == "unreadable" then
    -- un métier dont la « fenêtre » n'est pas un métier standard (archéologie) : le client accepte
    -- l'ouverture mais rien ne s'ouvre → l'addon l'ignore après 8 s et continue avec les autres.
    barValues = {}
    Cohors_Recipes()
    tick(4)
    local done = false
    for _ = 1, 4000 do
        if not tick(1) then break end
        if (M.t - 100000.0) % 4 < 0.26 then
            local ob = openButton()
            if ob and ob:IsEnabled() and ob._scripts["OnClick"] then
                M.hardware = true
                pcall(ob._scripts["OnClick"], ob, "LeftButton", false)
                M.hardware = false
            end
        end
        if Cohors_DB.recipes_total ~= nil then done = true; break end
    end
    check(done, "export terminé malgré un métier illisible (%.1f s virtuelles)", M.t - 100000.0)
    check(Cohors_DB.recipes_total == 5, "5 recettes lues (métier illisible ignoré) — total %s",
        tostring(Cohors_DB.recipes_total))
    check(chat_has("ne s'ouvre pas comme un métier standard"), "métier illisible signalé dans le chat")
    check((Cohors_DB.rec_diag or ""):find("métiers ignorés", 1, true) ~= nil,
        "métier ignoré consigné dans le rapport")

elseif scenario == "logout" then
    -- /reload ou déconnexion en plein export : ce qui est déjà lu doit être dans le fichier
    -- (sauvegardes incrémentales + PLAYER_LOGOUT). C'est LE cas qui a coûté 168 recettes à Fred.
    barValues = {}
    Cohors_Recipes()
    local saved = false
    for _ = 1, 4000 do
        if not tick(1) then break end
        if chat_has("OK — Alchimie") then
            pcall(driver._scripts["OnEvent"], driver, "PLAYER_LOGOUT")   -- /reload simulé
            saved = (Cohors_DB.recipes_at ~= nil) and
                    ((Cohors_DB.recipes or ""):find("Alchimie", 1, true) ~= nil)
            break
        end
    end
    check(saved, "PLAYER_LOGOUT en cours d'export : recettes déjà au fichier (%s caractères)",
        tostring(Cohors_DB.recipes and #Cohors_DB.recipes or 0))

elseif scenario == "slash" then
    -- v1.7.1 : « /cohors » sans argument ouvre le panneau, ne collecte RIEN tout seul.
    SlashCmdList["Cohors"]("")
    tick(30)
    check(not chat_has("lecture du calendrier"), "« /cohors » n'auto-collecte plus le calendrier")
    check(Cohors_DB.export == nil, "aucune collecte lancée par « /cohors »")
    -- icône de mini-carte (v1.11.0) : clic gauche = bascule du panneau
    local mmb, uif
    for _, fr in ipairs(frames) do
        if fr._name == "CohorsMinimapButton" then mmb = fr end
        if fr._name == "CohorsFrame" then uif = fr end
    end
    check(mmb ~= nil, "icône de mini-carte créée au chargement")
    if mmb and mmb._scripts["OnClick"] and uif then
        uif:Hide()
        mmb._scripts["OnClick"](mmb, "LeftButton")
        check(uif:IsShown(), "clic gauche sur l'icône : panneau ouvert")
        mmb._scripts["OnClick"](mmb, "LeftButton")
        check(not uif:IsShown(), "deuxième clic sur l'icône : panneau refermé")
    else
        check(false, "icône de mini-carte : clic non testable (mmb=%s frame=%s)",
            tostring(mmb), tostring(uif))
    end

elseif scenario == "wishlist" then
    -- v1.12.1 : un seul panneau à onglets — l'import se fait dans le champ partagé
    SlashCmdList["Cohors"]("wishlist")
    local uif
    for _, fr in ipairs(frames) do if fr._name == "CohorsFrame" then uif = fr end end
    check(uif ~= nil and uif:IsShown(), "« /cohors wishlist » ouvre le panneau (onglet Wishlist)")
    local wlEbF
    for i = #frames, 1, -1 do
        if frames[i]._type == "EditBox" then wlEbF = frames[i]; break end
    end
    check(wlEbF ~= nil, "champ partagé présent dans le panneau")

    -- import valide : une pièce + une recette
    wlEbF:SetText("CohorsWL1\n111|item|Épaule de test\n-42|recipe|Lame de test\n")
    Cohors_WLImport()
    check(#(Cohors_DB.wl or {}) == 2, "2 objets importés (trouvés : %d)", #(Cohors_DB.wl or {}))
    check(Cohors_DB.wl[1] and Cohors_DB.wl[1].n == "Épaule de test"
        and Cohors_DB.wl[2] and Cohors_DB.wl[2].t == "recipe",
        "pièce et recette lues correctement")
    check(chat_has("wishlist importée : 2 objet(s)"), "confirmation d'import dans le chat")

    -- import refusé : la liste précédente est CONSERVÉE
    wlEbF:SetText("bonjour, ceci n'est pas un export")
    Cohors_WLImport()
    check(#(Cohors_DB.wl or {}) == 2, "import invalide refusé sans écraser la liste")
    check(chat_has("import wishlist refusé"), "refus signalé dans le chat")

    -- lignes douteuses : les valides passent, les autres sont comptées
    wlEbF:SetText("CohorsWL1\n111|item|Épaule de test\n-42|recipe|Lame de test\nn'importe quoi\n")
    Cohors_WLImport()
    check(#(Cohors_DB.wl or {}) == 2 and (Cohors_DB.wl_bad or 0) == 1,
        "ligne illisible ignorée (bad=%s)", tostring(Cohors_DB.wl_bad))

    -- alerte d'instance : boss, pièce, recette (reconnue au nom « Plans : … »)
    C_ITEM_NAMES[222] = "Plans : Lame de test"
    C_ITEM_NAMES[333] = "Collier sans rapport"
    WL.instance = { name = "Raid de test", itype = "raid", mapID = 777, diff = "Héroïque", max = 20 }
    WL.jid = 42
    WL.loot = { { name = "Boss Alpha", items = { 111, 222, 333 } }, { name = "Boss Beta", items = { 333 } } }
    Cohors_WLZoneCheck()
    local alertTxt
    for _, fr in ipairs(frames) do
        if fr._type == "EditBox" and (fr._text or ""):find("Raid de test", 1, true) then alertTxt = fr._text end
    end
    check(alertTxt ~= nil, "fenêtre d'alerte affichée en entrant dans l'instance")
    if alertTxt then
        check(alertTxt:find("Boss Alpha", 1, true) ~= nil, "boss concerné listé")
        check(alertTxt:find("Épaule de test", 1, true) ~= nil, "pièce de la wishlist listée")
        check(alertTxt:find("Lame de test", 1, true) ~= nil, "recette reconnue au nom (« Plans : … »)")
        check(alertTxt:find("(recette)", 1, true) ~= nil, "la recette est signalée comme telle")
        check(alertTxt:find("Boss Beta", 1, true) == nil, "boss sans objet souhaité absent de la liste")
    end

    -- alerte coupée : aucun scan ; réactivée : le scan reprend
    Cohors_DB.wl_alert = false
    local before = Cohors_DB.wl_last
    WL.instance = { name = "Donjon de test", itype = "party", mapID = 778, diff = "Héroïque", max = 5 }
    Cohors_WLZoneCheck()
    check(Cohors_DB.wl_last == before, "alerte désactivée : aucune alerte")
    Cohors_DB.wl_alert = true
    Cohors_WLZoneCheck()
    check(Cohors_DB.wl_last ~= before, "alerte réactivée : scan de nouveau effectué")

    -- recette déjà apprise : plus signalée (sort connu côté client), la pièce reste listée
    C_ITEM_LINKS[222] = "item:222"
    C_ITEM_SPELLS["item:222"] = 555
    WL.known[555] = true
    WL.instance = { name = "Repaire de test", itype = "raid", mapID = 779, diff = "", max = 20 }
    Cohors_WLZoneCheck()
    local txt2
    for _, fr in ipairs(frames) do
        if fr._type == "EditBox" and (fr._text or ""):find("Repaire de test", 1, true) then txt2 = fr._text end
    end
    check(txt2 ~= nil and txt2:find("Épaule de test", 1, true) ~= nil,
        "la pièce reste signalée quand la recette est connue")
    check(txt2 ~= nil and txt2:find("Lame de test", 1, true) == nil,
        "recette déjà apprise : non signalée")

    -- hors instance, la commande manuelle le dit clairement
    WL.instance = nil
    SlashCmdList["Cohors"]("ici")
    check(chat_has("tu n'es pas dans une instance"), "« /cohors ici » hors instance : message clair")

    -- menu par onglets : les bons boutons selon l'onglet, et bascule de l'alerte
    SlashCmdList["Cohors"]("")
    local bWl, bAlert, bCal, bImp, bMenuCol
    for _, fr in ipairs(frames) do
        if fr._text == "Wishlist" then bWl = fr end
        if fr._text == "Alerte : oui" or fr._text == "Alerte : non" then bAlert = fr end
        if fr._text == "Calendrier" then bCal = fr end
        if fr._text == "Importer" then bImp = fr end
        if fr._text == "Collecter" then bMenuCol = fr end
    end
    check(bWl ~= nil, "bouton de menu « Wishlist » sur le panneau principal")
    check(bAlert ~= nil, "bouton « Alerte » sur le panneau principal")
    check(bImp ~= nil and bImp:IsShown() and bCal ~= nil and not bCal:IsShown(),
        "onglet Wishlist : « Importer » visible, les boutons du calendrier masqués")
    if bMenuCol and bMenuCol._scripts["OnClick"] then
        pcall(bMenuCol._scripts["OnClick"], bMenuCol, "LeftButton", false)
        check(bCal ~= nil and bCal:IsShown() and bImp ~= nil and not bImp:IsShown(),
            "clic menu « Collecter » : les boutons du calendrier remplacent ceux de la wishlist")
    end
    if bAlert and bAlert._scripts["OnClick"] then
        local t0 = bAlert:GetText()
        pcall(bAlert._scripts["OnClick"], bAlert, "LeftButton", false)
        check(bAlert:GetText() ~= t0, "le bouton alerte bascule (%s -> %s)", t0, bAlert:GetText())
    end

    -- « Vider » efface la liste de l'addon
    Cohors_WLClear()
    check(#(Cohors_DB.wl or {}) == 0, "« Vider » efface la liste de l'addon")

else
    -- full / apifail : export des recettes
    barValues = {}
    Cohors_Recipes()
    -- exclusion : la collecte calendrier doit être refusée pendant l'export recettes
    Cohors_Collect()
    check(chat_has("attends la fin avant de lancer le calendrier"), "calendrier refusé pendant l'export recettes")
    check(Cohors_DB.export == nil, "aucune collecte calendrier lancée pendant l'export")
    local ob0 = openButton()
    check(ob0 ~= nil and ob0:IsShown(), "bouton « Ouvrir » présent dès le départ")
    check(ob0 and (ob0:GetText() or ""):find("Alchimie", 1, true) ~= nil,
        "bouton pointe le 1er métier restant (texte : %s)", ob0 and ob0:GetText() or "?")
    if ob0 and ob0._scripts["OnClick"] then
        M.hardware = true
        pcall(ob0._scripts["OnClick"], ob0, "LeftButton", false)
        M.hardware = false
    end
    local done = false
    for _ = 1, 4000 do
        if not tick(1) then break end
        if Cohors_DB.recipes_total ~= nil then done = true; break end
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
        check(Cohors_DB.recipes_total == 7, "7 recettes attendues (2 métiers, une passe chacun) — trouvées %s",
            tostring(Cohors_DB.recipes_total))
        check((Cohors_DB.rec_diag or ""):find("1 non apprises", 1, true) ~= nil,
            "recette non apprise filtrée (rapport rec_diag)")
        check(not chat_has("chargement INCOMPLET"), "aucune alerte de chargement partiel")
        check(Cohors_DB.file_ok ~= nil, "marqueur de chargement complet posé (file_ok)")
        check(((openButton() and openButton():GetText()) or ""):find("terminé", 1, true) ~= nil,
            "bouton passé à « ✔ terminé » en fin d'export")
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
