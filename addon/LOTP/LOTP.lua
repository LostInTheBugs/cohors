-- LOTP — Calendrier de guilde → lotp.gensbien.fr
-- Collecte les événements de guilde (raids, invitations, réponses) et génère
-- une chaîne à coller sur le site (page Calendrier → « Importer »).
-- Commandes : /lotp  (collecter + fenêtre) · /lotp export · /lotp collect
local ADDON_VER = "1.0.1"
local WINDOW_DAYS = 21 -- fenêtre d'export : aujourd'hui → +21 j

LOTP_DB = LOTP_DB or {}

local f = CreateFrame("Frame")

local collecting = false
local queue, current = {}, nil -- événements à ouvrir, événement en cours
local results = {}             -- événements collectés

-- ---------------------------------------------------------------- utilitaires
local function msg(text)
    DEFAULT_CHAT_FRAME:AddMessage("|cffdfa55aLOTP|r " .. tostring(text))
end

local function statusText(s)
    if s == 0 or s == nil then return "en attente" end
    if s == 1 then return "dispo" end
    if s == 2 then return "non" end
    if s == 3 then return "confirmé" end
    if s == 4 then return "sorti" end
    if s == 5 then return "standby" end
    if s == 6 then return "inscrit" end
    if s == 7 then return "pas inscrit" end
    if s == 8 then return "incertain" end
    return "?"
end

local function jsonEsc(s)
    s = tostring(s or "")
    s = s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t")
    return s
end

local function dateStr(t)
    local ok, s = pcall(os.date, "%Y-%m-%d %H:%M", t)
    return ok and s or "?"
end

-- ------------------------------------------------------------------- collecte
local function buildExport()
    local parts = {}
    parts[#parts + 1] = '{"v":1,"ver":"' .. jsonEsc(ADDON_VER) .. '"'
    parts[#parts + 1] = ',"player":"' .. jsonEsc(UnitName("player") or "?") .. '"'
    parts[#parts + 1] = ',"realm":"' .. jsonEsc(GetRealmName() or "?") .. '"'
    parts[#parts + 1] = ',"at":' .. tostring(time()) .. ',"events":['
    for i, e in ipairs(results) do
        local ev = {}
        ev[#ev + 1] = '{"id":' .. tostring(e.id or 0)
        ev[#ev + 1] = ',"title":"' .. jsonEsc(e.title) .. '"'
        ev[#ev + 1] = ',"date":"' .. jsonEsc(e.date) .. '"'
        ev[#ev + 1] = ',"ts":' .. tostring(e.ts or 0)
        ev[#ev + 1] = ',"type":' .. tostring(e.etype or 0)
        ev[#ev + 1] = ',"inv":['
        for j, inv in ipairs(e.invites or {}) do
            if j > 1 then ev[#ev + 1] = "," end
            ev[#ev + 1] = '{"n":"' .. jsonEsc(inv.n) .. '","s":' .. tostring(inv.s or -1)
            if inv.t then ev[#ev + 1] = ',"t":' .. tostring(inv.t) end
            ev[#ev + 1] = "}"
        end
        ev[#ev + 1] = "]}"
        if i > 1 then parts[#parts + 1] = "," end
        parts[#parts + 1] = table.concat(ev)
    end
    parts[#parts + 1] = "]}"
    return table.concat(parts)
end

local function readOpenEvent()
    local ok, num = pcall(C_Calendar.GetNumInvites)
    local n = (ok and num) or 0
    local invs = {}
    for j = 1, n do
        local ok2, inv = pcall(C_Calendar.EventGetInvite, j)
        if ok2 and type(inv) == "table" then
            local name = inv.name or inv.invitee or inv.characterName
            local st = inv.status
            if st == nil then st = inv.inviteStatus end
            if name then
                local rt = nil
                local ok3, r = pcall(C_Calendar.EventGetInviteResponseTime, j)
                if ok3 and type(r) == "number" and r > 0 then rt = r end
                invs[#invs + 1] = { n = name, s = st or -1, t = rt }
            end
        end
    end
    return invs
end

local function finishCollect()
    collecting = false
    current = nil
    LOTP_DB.export = buildExport()
    LOTP_DB.export_at = time()
    LOTP_DB.player = UnitName("player")
    local nresp = 0
    for _, e in ipairs(results) do
        nresp = nresp + #(e.invites or {})
    end
    msg(("%d raid(s) collecté(s), %d réponse(s). • /lotp export pour la chaîne à coller sur le site.")
        :format(#results, nresp))
    if LOTP_Refresh then LOTP_Refresh() end
end

local function openNext()
    if #queue == 0 then
        finishCollect()
        return
    end
    local e = table.remove(queue, 1)
    current = e
    local ok = pcall(C_Calendar.OpenEvent, e.id)
    if not ok then
        -- repli : certains clients attendent (index) plutôt que (eventID)
        pcall(C_Calendar.OpenEvent, e.idx or 1)
    end
    C_Timer.After(3, function()
        if collecting and current == e then
            -- échec ou pas de réponse : on passe au suivant
            results[#results + 1] = { id = e.id, title = e.title, date = e.date, ts = e.ts,
                                      etype = e.etype, invites = {} }
            current = nil
            openNext()
        end
    end)
end

local function onEventList()
    if not collecting or current ~= nil then return end
    queue = {}
    local now = time()
    local maxT = now + WINDOW_DAYS * 86400
    local ok, n = pcall(C_Calendar.GetNumGuildEvents)
    n = (ok and n) or 0
    for i = 1, n do
        local ok2, e = pcall(C_Calendar.GetGuildEventInfo, i)
        if ok2 and type(e) == "table" then
            local ok3, t = pcall(os.time, { year = e.year, month = e.month, day = e.monthDay,
                                            hour = e.hour, min = e.minute })
            if ok3 and t and t >= now - 86400 and t <= maxT then
                queue[#queue + 1] = { id = e.eventID or 0, idx = i, title = e.title or "?",
                                      date = dateStr(t), ts = t, etype = e.eventType or 0 }
            end
        end
    end
    if #queue == 0 then
        finishCollect()
        return
    end
    msg(("%d événement(s) à collecter…"):format(#queue))
    openNext()
end

function LOTP_Collect()
    if collecting then
        msg("collecte déjà en cours…")
        return
    end
    collecting = true
    current = nil
    queue = {}
    results = {}
    msg("lecture du calendrier de guilde…")
    pcall(C_Calendar.OpenCalendar)
    -- la liste arrive via CALENDAR_UPDATE_EVENT_LIST ; repli si l'événement ne vient pas
    C_Timer.After(1.5, function()
        if collecting and current == nil and #queue == 0 then
            onEventList()
        end
    end)
end

f:SetScript("OnEvent", function(_, event, arg1)
    if event == "ADDON_LOADED" then
        if arg1 == "LOTP" then
            local ok, _, _, _, iface = pcall(GetBuildInfo)
            if not ok or type(iface) ~= "number" then iface = "?" end
            if not LOTP_DB.export then
                msg(("v%s chargée (client %s) — /lotp pour collecter le calendrier de guilde.")
                    :format(ADDON_VER, tostring(iface)))
            else
                msg(("v%s chargée (client %s) — dernier export : %s • /lotp pour ouvrir.")
                    :format(ADDON_VER, tostring(iface), dateStr(LOTP_DB.export_at or 0)))
            end
        end
    elseif event == "CALENDAR_UPDATE_EVENT_LIST" then
        onEventList()
    elseif event == "CALENDAR_OPEN_EVENT" then
        if collecting and current ~= nil then
            local e = current
            local invs = readOpenEvent()
            results[#results + 1] = { id = e.id, title = e.title, date = e.date, ts = e.ts,
                                      etype = e.etype, invites = invs }
            pcall(C_Calendar.CloseEvent)
            current = nil
            C_Timer.After(0.2, openNext)
        end
    end
end)
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("CALENDAR_UPDATE_EVENT_LIST")
f:RegisterEvent("CALENDAR_OPEN_EVENT")

-- ------------------------------------------------------------------------ UI
local ui = CreateFrame("Frame", "LOTPFrame", UIParent, BackdropTemplateMixin and "BackdropTemplate" or nil)
ui:SetSize(720, 500)
ui:SetPoint("CENTER")
ui:SetFrameStrata("DIALOG")
ui:SetMovable(true)
ui:EnableMouse(true)
ui:SetClampedToScreen(true)
ui:RegisterForDrag("LeftButton")
ui:SetScript("OnDragStart", ui.StartMoving)
ui:SetScript("OnDragStop", ui.StopMovingOrSizing)
if ui.SetBackdrop then
    ui:SetBackdrop({
        bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
        edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
        tile = true, tileSize = 16, edgeSize = 24,
        insets = { left = 4, right = 4, top = 4, bottom = 4 },
    })
end
ui:Hide()

local title = ui:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
title:SetPoint("TOP", 0, -14)
title:SetText("LOTP — Calendrier de guilde")

local sub = ui:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
sub:SetPoint("TOP", 0, -36)
sub:SetText("Collecte les raids + réponses, puis colle la chaîne exportée sur lotp.gensbien.fr (page Calendrier).")

local eb = CreateFrame("EditBox", nil, ui)
eb:SetMultiLine(true)
eb:SetSize(680, 360)
eb:SetPoint("TOPLEFT", 20, -60)
eb:SetFontObject(ChatFontNormal)
eb:SetAutoFocus(false)
eb:SetTextInsets(6, 6, 6, 6)
eb:SetText("")
eb:SetScript("OnEscapePressed", function() eb:ClearFocus() end)

local function mkButton(text, x, w, fn)
    local b = CreateFrame("Button", nil, ui, "UIPanelButtonTemplate")
    b:SetSize(w, 26)
    b:SetPoint("BOTTOMLEFT", x, 16)
    b:SetText(text)
    b:SetScript("OnClick", fn)
    return b
end

local function showSummary()
    local lines = {}
    if not LOTP_DB.export then
        lines[#lines + 1] = "Aucune collecte pour le moment — clique « Collecter »."
    else
        local nresp = 0
        for _, e in ipairs(results) do nresp = nresp + #(e.invites or {}) end
        lines[#lines + 1] = ("Dernière collecte : %s · %d réponse(s).")
            :format(LOTP_DB.export_at and dateStr(LOTP_DB.export_at) or "?", nresp)
        for _, e in ipairs(results) do
            local c = { ok = 0, maybe = 0, no = 0, wait = 0 }
            local waiting = {}
            for _, inv in ipairs(e.invites or {}) do
                if inv.s == 1 or inv.s == 3 then c.ok = c.ok + 1
                elseif inv.s == 8 then c.maybe = c.maybe + 1
                elseif inv.s == 2 then c.no = c.no + 1
                else
                    c.wait = c.wait + 1
                    if #waiting < 25 then waiting[#waiting + 1] = inv.n end
                end
            end
            lines[#lines + 1] = ("%s  %s  —  %d dispo · %d incertain · %d non · %d sans réponse")
                :format(e.date or "?", e.title or "?", c.ok, c.maybe, c.no, c.wait)
            if #waiting > 0 then
                lines[#lines + 1] = "   en attente : " .. table.concat(waiting, ", ")
            end
        end
        lines[#lines + 1] = ""
        lines[#lines + 1] = "Clique « Exporter » puis Ctrl+A / Ctrl+C pour copier la chaîne à coller sur le site."
    end
    eb:SetText(table.concat(lines, "\n"))
    eb:HighlightText()
    eb:SetFocus()
end

function LOTP_Refresh()
    if ui:IsShown() and not collecting then
        showSummary()
    end
end

mkButton("Collecter", 20, 100, function() LOTP_Collect() end)
mkButton("Exporter", 128, 100, function()
    if not LOTP_DB.export then
        msg("rien à exporter pour le moment — clique « Collecter ».")
        return
    end
    eb:SetText(LOTP_DB.export)
    eb:HighlightText()
    eb:SetFocus()
    msg("chaîne sélectionnée — fais Ctrl+C puis colle-la sur lotp.gensbien.fr (page Calendrier).")
end)
mkButton("Fermer", 236, 100, function() ui:Hide() end)

SLASH_LOTP1 = "/lotp"
SlashCmdList["LOTP"] = function(arg)
    arg = (arg or ""):lower()
    if arg == "" then
        ui:Show()
        if LOTP_DB.export then pcall(showSummary) end
        if not collecting and (not LOTP_DB.export or (time() - (LOTP_DB.export_at or 0)) > 3600) then
            LOTP_Collect()
        end
    elseif arg == "collect" then
        ui:Show()
        LOTP_Collect()
    elseif arg == "export" then
        ui:Show()
        if LOTP_DB.export then
            eb:SetText(LOTP_DB.export)
            eb:HighlightText()
            eb:SetFocus()
        else
            msg("aucune donnée — /lotp collect d'abord.")
        end
    else
        msg("commandes : /lotp · /lotp collect · /lotp export")
    end
end
