-- LOTP — Calendrier de guilde → lotp.gensbien.fr
-- Collecte les événements de guilde (raids, invitations, réponses) et génère
-- une chaîne à coller sur le site (page Calendrier → « Importer »).
-- Commandes : /lotp · /lotp collect · /lotp export · /lotp diag
local ADDON_VER = "1.2.0"
local WINDOW_DAYS = 21       -- fenêtre d'export : aujourd'hui → +21 j
local MAX_READ_TRIES = 12    -- tentatives de lecture (~30 s)
local EVENT_TIMEOUT = 4      -- délai max par événement ouvert (s)
local MAX_EVENTS = 40

LOTP_DB = LOTP_DB or {}

local f = CreateFrame("Frame")

local collecting = false
local started = false
local current = nil
local queue = {}
local results = {}
local readTries = 0
local source = nil -- "club" | "guild"

-- ---------------------------------------------------------------- utilitaires
local function msg(text)
    DEFAULT_CHAT_FRAME:AddMessage("|cffdfa55aLOTP|r " .. tostring(text))
end

local function clientIface()
    local ok, _, _, _, iface = pcall(GetBuildInfo)
    if not ok or type(iface) ~= "number" then return "?" end
    return iface
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

local function calTime(epoch)
    local ok, tt = pcall(date, "*t", epoch)
    if not ok or type(tt) ~= "table" then return nil end
    return { year = tt.year, month = tt.month, monthDay = tt.day, hour = tt.hour, minute = tt.min, weekday = tt.wday }
end

local function timeFields(st)
    -- st = CalendarTime (table) → date "YYYY-MM-DD HH:MM" + epoch si possible
    if type(st) ~= "table" or not st.year then return "?", 0 end
    local day = st.monthDay or st.day or 0
    local date = ("%04d-%02d-%02d %02d:%02d"):format(st.year, st.month or 0, day, st.hour or 0, st.minute or 0)
    local ts = 0
    local ok, t = pcall(os.time, { year = st.year, month = st.month, day = day, hour = st.hour or 0, min = st.minute or 0 })
    if ok and t then ts = t end
    return date, ts
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

local function readInvites()
    local invs = {}
    local ok, num = pcall(C_Calendar.GetNumInvites)
    local n = (ok and num) or 0
    for j = 1, n do
        local ok2, inv = pcall(C_Calendar.EventGetInvite, j)
        if ok2 and type(inv) == "table" then
            local okn, name = pcall(function() return inv.name or inv.invitee or inv.characterName end)
            local okS, st = pcall(function() return inv.inviteStatus or inv.status end)
            if okn and name and tostring(name) ~= "" then
                local entry = { n = tostring(name), s = (okS and tonumber(st)) or -1 }
                local ok3, r = pcall(C_Calendar.EventGetInviteResponseTime, j)
                if ok3 and type(r) == "number" and r > 0 then entry.t = r end
                invs[#invs + 1] = entry
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
    if #results == 0 then
        msg("aucun événement trouvé. Vérifie dans le calendrier du jeu (bouton Calendrier) que la guilde "
            .. "a bien des raids à venir, puis /lotp pour réessayer. (/lotp diag pour le détail)")
    else
        msg(("%d raid(s) collecté(s), %d réponse(s). • /lotp export pour la chaîne à coller sur le site.")
            :format(#results, nresp))
    end
    if LOTP_Refresh then LOTP_Refresh() end
end

-- Résout (offsetMois, jour, index) pour pouvoir ouvrir un événement.
local function resolveIndex(ev)
    if ev.listIndex then
        local ok, info = pcall(C_Calendar.GetGuildEventSelectionInfo, ev.listIndex)
        if ok and type(info) == "table" and info.monthDay then return info end
    end
    if ev.eventID and ev.eventID > 0 then
        local ok, info = pcall(C_Calendar.GetEventIndexInfo, ev.eventID)
        if ok and type(info) == "table" and info.monthDay then return info end
        -- repli : chercher par jour dans les mois proches
        local y, m, d = (ev.date or ""):match("^(%d+)%-(%d+)%-(%d+)")
        local ok2, mi = pcall(C_Calendar.GetMonthInfo, 0)
        if y and m and d and ok2 and type(mi) == "table" and mi.month and mi.year then
            local off = (tonumber(y) * 12 + tonumber(m)) - (mi.year * 12 + mi.month)
            if off >= 0 and off <= 4 then
                local day = tonumber(d)
                local ok3, info2 = pcall(C_Calendar.GetEventIndexInfo, ev.eventID, off, day)
                if ok3 and type(info2) == "table" and info2.eventIndex then return info2 end
                local ok4, nde = pcall(C_Calendar.GetNumDayEvents, off, day)
                for idx = 1, (ok4 and nde or 0) do
                    local ok5, de = pcall(C_Calendar.GetDayEvent, off, day, idx)
                    if ok5 and type(de) == "table" and de.eventID == ev.eventID then
                        return { offsetMonths = off, monthDay = day, eventIndex = idx }
                    end
                end
            end
        end
    end
    return nil
end

local function readOpenEvent()
    local e = current
    local invs = readInvites()
    results[#results + 1] = { id = e.eventID, title = e.title, date = e.date, ts = e.ts,
                              etype = e.etype, invites = invs }
    pcall(C_Calendar.CloseEvent)
    current = nil
    C_Timer.After(0.2, function() LOTP_OpenNext() end)
end

function LOTP_OpenNext()
    if not collecting then return end
    if #queue == 0 then
        finishCollect()
        return
    end
    local e = table.remove(queue, 1)
    local info = resolveIndex(e)
    if not info or not info.monthDay or not info.eventIndex then
        results[#results + 1] = { id = e.eventID, title = e.title, date = e.date, ts = e.ts,
                                  etype = e.etype, invites = {} }
        C_Timer.After(0.1, function() LOTP_OpenNext() end)
        return
    end
    current = e
    local ok = pcall(C_Calendar.OpenEvent, info.offsetMonths or 0, info.monthDay, info.eventIndex)
    if not ok then
        results[#results + 1] = { id = e.eventID, title = e.title, date = e.date, ts = e.ts,
                                  etype = e.etype, invites = {} }
        current = nil
        C_Timer.After(0.1, function() LOTP_OpenNext() end)
        return
    end
    C_Timer.After(EVENT_TIMEOUT, function()
        if collecting and current == e then
            results[#results + 1] = { id = e.eventID, title = e.title, date = e.date, ts = e.ts,
                                      etype = e.etype, invites = {} }
            current = nil
            LOTP_OpenNext()
        end
    end)
end

-- Source 1 : événements du club de guilde (C_Calendar.GetClubCalendarEvents).
local function guildClubId()
    local ok, id = pcall(C_Club.GetGuildClubId)
    if ok and id then return id end
    local ok2, clubs = pcall(C_Club.GetSubscribedClubs)
    if ok2 and type(clubs) == "table" then
        for _, c in ipairs(clubs) do
            if c.clubId and c.clubType and Enum and Enum.ClubType and c.clubType == Enum.ClubType.Guild then
                return c.clubId
            end
        end
        if clubs[1] and clubs[1].clubId then return clubs[1].clubId end
    end
    return nil
end

local function gatherClub()
    local clubId = guildClubId()
    if not clubId then return nil end
    local t0, t1 = calTime(time() - 86400), calTime(time() + WINDOW_DAYS * 86400)
    if not t0 or not t1 then return nil end
    local ok, evs = pcall(C_Calendar.GetClubCalendarEvents, clubId, t0, t1)
    if not ok or type(evs) ~= "table" or #evs == 0 then return nil end
    local out = {}
    for _, e in ipairs(evs) do
        if type(e) == "table" then
            local date, ts = timeFields(e.startTime)
            out[#out + 1] = { eventID = e.eventID or 0, title = e.title or "?", date = date,
                              ts = ts, etype = e.eventType or 0 }
            if #out >= MAX_EVENTS then break end
        end
    end
    return #out > 0 and out or nil
end

-- Source 2 : liste des invitations de guilde (C_Calendar.GetNumGuildEvents).
local function gatherGuildList()
    local ok, cnt = pcall(C_Calendar.GetNumGuildEvents)
    local n = (ok and type(cnt) == "number") and cnt or 0
    if n <= 0 then return nil end
    local out = {}
    for i = 1, n do
        local ok2, e = pcall(C_Calendar.GetGuildEventInfo, i)
        if ok2 and type(e) == "table" then
            local day = e.monthDay or e.day
            local mi = e.minute or e.min or 0
            local hh = e.hour or 0
            local date, ts = "?", 0
            if day and e.year and e.month then
                date = ("%04d-%02d-%02d %02d:%02d"):format(e.year, e.month, day, hh, mi)
                local ok3, t = pcall(os.time, { year = e.year, month = e.month, day = day, hour = hh, min = mi })
                if ok3 and t then ts = t end
            end
            out[#out + 1] = { eventID = e.eventID or 0, title = e.title or "?", date = date,
                              ts = ts, etype = e.eventType or 0, listIndex = i }
            if #out >= MAX_EVENTS then break end
        end
    end
    return #out > 0 and out or nil
end

local function gatherAny()
    local out = gatherClub()
    if out then
        source = "club"
        return out
    end
    out = gatherGuildList()
    if out then
        source = "guild"
        return out
    end
    return nil
end

local function tryStart()
    if not collecting or started or current ~= nil or #queue > 0 then return end
    local list = gatherAny()
    if not list then return end
    -- filtre : événements d'hier ou plus récents (garde ceux sans date exploitable)
    local now = time()
    local okd, minDay = pcall(os.date, "%Y-%m-%d", now - 86400)
    minDay = okd and minDay or nil
    queue = {}
    for _, e in ipairs(list) do
        local fresh = true
        if e.date and e.date ~= "?" and minDay then
            fresh = e.date:sub(1, 10) >= minDay
        elseif e.ts and e.ts > 0 then
            fresh = e.ts >= now - 86400
        end
        if fresh then queue[#queue + 1] = e end
    end
    if #queue == 0 then
        -- la liste existe mais tout est passé : on garde tout de même les 3 derniers
        for i = 1, math.min(3, #list) do queue[i] = list[i] end
    end
    if #queue == 0 then return end
    started = true
    msg(("%d événement(s) à collecter (%s)…"):format(#queue, source or "?"))
    LOTP_OpenNext()
end

local function poll()
    if not collecting or started then return end
    readTries = readTries + 1
    tryStart()
    if started then return end
    if readTries > MAX_READ_TRIES then
        tryStart()
        if not started then finishCollect() end
        return
    end
    if readTries == 4 or readTries == 8 then
        pcall(C_Calendar.OpenCalendar)
    end
    C_Timer.After(2.5, poll)
end

function LOTP_Collect()
    if collecting then
        msg("collecte déjà en cours…")
        return
    end
    collecting = true
    started = false
    current = nil
    queue = {}
    results = {}
    readTries = 0
    source = nil
    msg("interrogation du calendrier de guilde…")
    pcall(C_Calendar.OpenCalendar)
    C_Timer.After(1.0, poll)
end

f:SetScript("OnEvent", function(_, event, arg1)
    if event == "ADDON_LOADED" then
        if arg1 == "LOTP" then
            if not LOTP_DB.export then
                msg(("v%s chargée (client %s) — /lotp pour collecter le calendrier de guilde.")
                    :format(ADDON_VER, tostring(clientIface())))
            else
                msg(("v%s chargée (client %s) — dernier export : %s • /lotp pour ouvrir.")
                    :format(ADDON_VER, tostring(clientIface()), dateStr(LOTP_DB.export_at or 0)))
            end
        end
    elseif event == "CALENDAR_UPDATE_EVENT_LIST" then
        if collecting then tryStart() end
    elseif event == "CALENDAR_OPEN_EVENT" then
        if collecting and current ~= nil then
            C_Timer.After(0.4, function()
                if collecting and current ~= nil then readOpenEvent() end
            end)
        end
    end
end)
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("CALENDAR_UPDATE_EVENT_LIST")
f:RegisterEvent("CALENDAR_OPEN_EVENT")

-- ------------------------------------------------------------------ diagnostic
local function dumpDiag()
    msg(("LOTP v%s · client %s · collecte %s"):format(ADDON_VER, tostring(clientIface()),
        collecting and "en cours" or "au repos"))
    local okC, clubId = pcall(guildClubId)
    msg("clubId guilde = " .. tostring(okC and clubId or "erreur"))
    local ok, cnt = pcall(C_Calendar.GetNumGuildEvents)
    msg("GetNumGuildEvents = " .. tostring(ok and cnt or "erreur"))
    if okC and clubId then
        local t0, t1 = calTime(time() - 86400), calTime(time() + WINDOW_DAYS * 86400)
        local ok2, evs = pcall(C_Calendar.GetClubCalendarEvents, clubId, t0, t1)
        local n2 = (ok2 and type(evs) == "table") and #evs or nil
        msg("GetClubCalendarEvents = " .. tostring(n2 or (ok2 and "vide" or "erreur")))
        if n2 and n2 > 0 then
            for i = 1, math.min(2, n2) do
                local e = evs[i]
                local date = timeFields(e.startTime)
                msg(("#%d %s | %s | eventID=%s"):format(i, tostring(e.title), tostring(date),
                    tostring(e.eventID)))
            end
        end
    end
    local n = (ok and type(cnt) == "number") and cnt or 0
    for i = 1, math.min(2, n) do
        local ok2, e = pcall(C_Calendar.GetGuildEventInfo, i)
        if ok2 and type(e) == "table" then
            local parts = {}
            for k, v in pairs(e) do
                parts[#parts + 1] = k .. "=" .. tostring(v)
            end
            table.sort(parts)
            msg(("#g%d %s"):format(i, table.concat(parts, " ")))
        end
    end
    local ok3, ni = pcall(C_Calendar.GetNumInvites)
    msg("GetNumInvites (événement ouvert) = " .. tostring(ok3 and ni or "erreur"))
    local ok4, mi = pcall(C_Calendar.GetMonthInfo, 0)
    if ok4 and type(mi) == "table" then
        msg(("mois affiché : %s/%s (%s jours)"):format(tostring(mi.month), tostring(mi.year), tostring(mi.numDays)))
    end
end

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
mkButton("Diag", 236, 80, function()
    dumpDiag()
end)
mkButton("Fermer", 324, 100, function() ui:Hide() end)

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
    elseif arg == "diag" then
        dumpDiag()
    else
        msg("commandes : /lotp · /lotp collect · /lotp export · /lotp diag")
    end
end
