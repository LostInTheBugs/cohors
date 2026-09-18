-- LOTP — Calendrier de guilde → lotp.gensbien.fr
-- Collecte les événements de guilde (raids, invitations, réponses) et génère
-- une chaîne à coller sur le site (page Calendrier → « Importer »).
-- Commandes : /lotp · /lotp collect · /lotp export · /lotp diag
--
-- Lecture du calendrier : même méthode que l'UI Blizzard — on affiche le mois
-- (SetAbsMonth/SetMonth) puis on lit les jours (GetNumDayEvents/GetDayEvent),
-- et on ouvre chaque événement (OpenEvent(0, jour, index)) pour les réponses.
local ADDON_VER = "1.3.2"
local WINDOW_DAYS = 21
local MAX_EVENTS = 40
local STEP_WAIT = 1.0    -- attente avant lecture d'un mois (s)
local EVENT_WAIT = 0.6   -- attente avant ouverture d'un événement (s)
local COLLECT_TIMEOUT = 75

LOTP_DB = LOTP_DB or {}

local f = CreateFrame("Frame")

local collecting = false
local queue = {}
local results = {}
local openedFrame = false
local watchMonth = nil    -- mois affiché pendant la collecte (pour restaurer)
local current = nil
local collectSeq = 0      -- identifiant de collecte (anti-vieux-timers)
local collectStartAt = 0

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

-- étiquette de date depuis un CalendarTime (table) du client
local function timeFields(st)
    if type(st) ~= "table" then return "?", 0 end
    local y = st.year or st.y
    local mo = st.month or st.mo
    local d = st.monthDay or st.day
    local h = st.hour or 0
    local mi = st.minute or st.min or 0
    if not (y and mo and d) then return "?", 0 end
    local okf, date = pcall(string.format, "%04d-%02d-%02d %02d:%02d", y, mo, d, h, mi)
    if not okf then return "?", 0 end
    local ts = 0
    local ok, t = pcall(os.time, { year = y, month = mo, day = d, hour = h, min = mi })
    if ok and t then ts = t end
    return date, ts
end

local function nowCalendarTime()
    local ok, ct = pcall(C_DateAndTime.GetCurrentCalendarTime)
    if ok and type(ct) == "table" and ct.month then return ct end
    local nt = date("*t", time())
    return { year = nt.year, month = nt.month, monthDay = nt.day, hour = nt.hour, minute = nt.min }
end

local function openCalendarFrame()
    local ok, shown = pcall(function() return CalendarFrame and CalendarFrame:IsShown() end)
    if ok and not shown and CalendarFrame then
        openedFrame = true
        pcall(ToggleCalendar)
    end
end

local function restoreCalendar()
    if watchMonth then
        pcall(C_Calendar.SetAbsMonth, watchMonth.month, watchMonth.year)
        watchMonth = nil
    end
    if openedFrame then
        openedFrame = false
        pcall(ToggleCalendar)
    end
end

-- trace progressive : visible dans LOTP_DB.trace (fichier SavedVariables)
local function dtrace(s)
    LOTP_DB.trace = tostring(LOTP_DB.trace or ("trace — " .. dateStr(time()))) .. "\n- " .. tostring(s)
end

local diagLines   -- définie plus bas (section diagnostic)
local finishCollect -- définie plus bas (section collecte)

-- garde-fou : capture les erreurs d'une étape, les rend visibles et termine proprement
local function guard(name, fn)
    local seq0 = collectSeq
    return function(...)
        local ok, err = pcall(fn, ...)
        if not ok then
            local e = tostring(err)
            LOTP_DB.last_error = name .. " : " .. e
            dtrace("ERREUR " .. name .. " : " .. e)
            msg("erreur (" .. name .. ") — " .. e)
            if collecting and collectSeq == seq0 then
                pcall(finishCollect)
            end
        end
    end
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
                invs[#invs + 1] = entry
            end
        end
    end
    return invs
end

finishCollect = function()
    if not collecting then return end
    collecting = false
    current = nil
    restoreCalendar()
    LOTP_DB.export = buildExport()
    LOTP_DB.export_at = time()
    LOTP_DB.player = UnitName("player")
    -- rapport de diagnostic : écrit automatiquement à chaque collecte
    local okd, lines = pcall(diagLines)
    if okd and type(lines) == "table" then
        lines[#lines + 1] = ("résultat : %d événement(s) collecté(s)"):format(#results)
        for i, e in ipairs(results) do
            if i <= 10 then
                lines[#lines + 1] = ("  collecté : %s | %s | %d réponse(s)"):format(
                    tostring(e.date), tostring(e.title), #(e.invites or {}))
            end
        end
        lines[#lines + 1] = ""
        lines[#lines + 1] = "— trace —"
        lines[#lines + 1] = tostring(LOTP_DB.trace or "?")
        LOTP_DB.diag = table.concat(lines, "\n")
        LOTP_DB.diag_at = time()
    end
    local nresp = 0
    for _, e in ipairs(results) do
        nresp = nresp + #(e.invites or {})
    end
    if #results == 0 then
        msg("aucun événement trouvé dans le calendrier. Ouvre le calendrier du jeu (touche C) pour vérifier "
            .. "que la guilde a bien des raids, puis /lotp. (le rapport a été enregistré : /reload puis "
            .. "envoie le fichier LOTP.lua)")
    else
        msg(("%d raid(s) collecté(s), %d réponse(s). • /lotp export pour la chaîne à coller sur le site.")
            :format(#results, nresp))
    end
    if LOTP_Refresh then LOTP_Refresh() end
end

-- Scan du mois affiché : renvoie les événements avec (shift du mois, jour, index).
local function scanViewedMonth(shift)
    local out = {}
    local okM, mi = pcall(C_Calendar.GetMonthInfo, 0)
    local numDays = 31
    if okM and type(mi) == "table" then
        local nd = tonumber(mi.numDays)
        if nd then numDays = nd end
    end
    for day = 1, numDays do
        local okN, n = pcall(C_Calendar.GetNumDayEvents, 0, day)
        local n2 = tonumber(okN and n or 0) or 0
        for i = 1, n2 do
            local okE, e = pcall(C_Calendar.GetDayEvent, 0, day, i)
            if okE and type(e) == "table" and (e.calendarType or "") ~= "HOLIDAY" then
                local date, ts = timeFields(e.startTime)
                out[#out + 1] = { id = e.eventID or 0, title = e.title or "?", date = date, ts = ts,
                                  etype = e.eventType or 0, shift = shift, day = day, idx = i }
            end
        end
    end
    return out
end

local function filterWindow(list)
    local now = time()
    local limit = now + WINDOW_DAYS * 86400
    local out = {}
    for _, e in ipairs(list) do
        local keep
        if e.ts and e.ts > 0 then
            keep = e.ts >= now - 86400 and e.ts <= limit
        else
            keep = true -- date illisible : on garde (le site filtrera)
        end
        if keep then out[#out + 1] = e end
        if #out >= MAX_EVENTS then break end
    end
    return out
end

-- Positionne la vue du calendrier sur le mois voulu puis ouvre les événements un par un.
local function positionView(shift)
    local now = nowCalendarTime()
    pcall(C_Calendar.SetAbsMonth, now.month, now.year)
    if (shift or 0) > 0 then
        pcall(C_Calendar.SetMonth, shift)
    end
end

local function openNext()
    if not collecting then return end
    if #queue == 0 then
        dtrace("ouvertures terminées")
        finishCollect()
        return
    end
    local e = table.remove(queue, 1)
    positionView(e.shift)
    C_Timer.After(EVENT_WAIT, guard("ouverture", function()
        if not collecting then return end
        current = e
        dtrace(("ouverture #%s : jour %s idx %s (%s)"):format(tostring(e.id), tostring(e.day),
            tostring(e.idx), tostring(e.title)))
        local okc, opened = pcall(C_Calendar.OpenEvent, 0, e.day, e.idx)
        if (not okc) or opened == false then
            dtrace("  → ouverture impossible (" .. tostring(okc and "refusée" or opened) .. ")")
            results[#results + 1] = { id = e.id, title = e.title, date = e.date, ts = e.ts,
                                      etype = e.etype, invites = {} }
            current = nil
            C_Timer.After(0.1, function() if collecting then openNext() end end)
            return
        end
        C_Timer.After(2.5, guard("ouverture (délai)", function()
            if collecting and current == e then
                dtrace("  → pas de réponse du calendrier (délai)")
                results[#results + 1] = { id = e.id, title = e.title, date = e.date, ts = e.ts,
                                          etype = e.etype, invites = {} }
                current = nil
                C_Timer.After(0.1, function() if collecting then openNext() end end)
            end
        end))
    end))
end

-- Phase 1 : balayage des mois (courant → +21 j). Phase 2 : ouverture des événements.
local function startCollect()
    local now = nowCalendarTime()
    watchMonth = { month = now.month, year = now.year }
    dtrace(("vue calendrier : mois %s/%s"):format(tostring(now.month), tostring(now.year)))
    openCalendarFrame()
    pcall(C_Calendar.OpenCalendar)
    pcall(C_Calendar.SetAbsMonth, now.month, now.year)
    local maxShift = 2
    local endT = time() + WINDOW_DAYS * 86400
    local okA, ctEnd = pcall(C_DateAndTime.GetCalendarTimeFromEpoch, endT)
    if okA and type(ctEnd) == "table" and ctEnd.month and ctEnd.year then
        maxShift = math.max(0, math.min(3, (ctEnd.year * 12 + ctEnd.month) - (now.year * 12 + now.month)))
    end
    C_Timer.After(STEP_WAIT, guard("balayage", function()
        if not collecting then return end
        local scanned = {}
        local shift = 0
        local function scanNext()
            if not collecting then return end
            if shift > maxShift then
                dtrace(("balayage fini : %d événement(s) brut(s)"):format(#scanned))
                local list = filterWindow(scanned)
                if #list == 0 then
                    dtrace("aucun événement — nouvelle tentative après chargement")
                    C_Timer.After(2.0, guard("balayage (relance)", function()
                        if not collecting then return end
                        local again = {}
                        for s = 0, maxShift do
                            positionView(s)
                            for _, e in ipairs(scanViewedMonth(s)) do again[#again + 1] = e end
                        end
                        queue = filterWindow(again)
                        if #queue == 0 then
                            finishCollect()
                        else
                            dtrace(("relance : %d à ouvrir"):format(#queue))
                            msg(("%d événement(s) à collecter…"):format(#queue))
                            openNext()
                        end
                    end))
                    return
                end
                queue = list
                dtrace(("%d événement(s) à ouvrir"):format(#queue))
                msg(("%d événement(s) à collecter…"):format(#queue))
                openNext()
                return
            end
            positionView(shift)
            C_Timer.After(shift == 0 and 0.6 or STEP_WAIT, guard("balayage (mois " .. tostring(shift) .. ")", function()
                if not collecting then return end
                local found = scanViewedMonth(shift)
                dtrace(("mois +%d : %d événement(s)"):format(shift, #found))
                for _, e in ipairs(found) do scanned[#scanned + 1] = e end
                shift = shift + 1
                scanNext()
            end))
        end
        scanNext()
    end))
end

function LOTP_Collect()
    if collecting then
        if (time() - (collectStartAt or 0)) > 60 then
            msg("collecte précédente bloquée — réinitialisation…")
            dtrace("réinitialisation (collecte bloquée)")
            collecting = false
        else
            msg("collecte déjà en cours…")
            return
        end
    end
    collectSeq = collectSeq + 1
    collecting = true
    collectStartAt = time()
    queue = {}
    results = {}
    current = nil
    openedFrame = false
    watchMonth = nil
    LOTP_DB.last_error = nil
    LOTP_DB.trace = ("trace — %s (addon v%s · client %s)"):format(dateStr(time()), ADDON_VER,
        tostring(clientIface()))
    dtrace("collecte démarrée")
    local mySeq = collectSeq
    C_Timer.After(COLLECT_TIMEOUT, guard("chien de garde", function()
        if collecting and collectSeq == mySeq then
            dtrace("chien de garde : délai dépassé")
            msg("collecte interrompue (délai dépassé) — le rapport a été enregistré.")
            finishCollect()
        end
    end))
    msg("lecture du calendrier de guilde…")
    startCollect()
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
    elseif event == "CALENDAR_OPEN_EVENT" then
        if collecting and current ~= nil then
            C_Timer.After(0.4, guard("lecture des réponses", function()
                if collecting and current ~= nil then
                    local e = current
                    local invs = readInvites()
                    dtrace(("  → %d réponse(s) lue(s)"):format(#invs))
                    results[#results + 1] = { id = e.id, title = e.title, date = e.date, ts = e.ts,
                                              etype = e.etype, invites = invs }
                    pcall(C_Calendar.CloseEvent)
                    current = nil
                    C_Timer.After(0.2, function() if collecting then openNext() end end)
                end
            end))
        end
    end
end)
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("CALENDAR_OPEN_EVENT")

-- ------------------------------------------------------------------ diagnostic
diagLines = function()
    local L = {}
    L[#L + 1] = "LOTP diag — " .. dateStr(time())
    L[#L + 1] = ("addon v%s · client %s · collecte %s"):format(ADDON_VER, tostring(clientIface()),
        collecting and ("en cours depuis " .. dateStr(collectStartAt)) or "au repos")
    L[#L + 1] = ("joueur %s — %s"):format(tostring(UnitName("player")), tostring(GetRealmName()))
    L[#L + 1] = ("CalendarFrame : %s"):format(CalendarFrame and (CalendarFrame:IsShown() and "affiché" or "existe (fermé)") or "absent")
    if LOTP_DB.last_error then
        L[#L + 1] = "dernière erreur : " .. tostring(LOTP_DB.last_error)
    end
    local okQ, gt = pcall(C_DateAndTime.GetCurrentCalendarTime)
    if okQ and type(gt) == "table" then
        L[#L + 1] = ("heure calendrier : %s-%s-%s %s:%s"):format(tostring(gt.year), tostring(gt.month),
            tostring(gt.monthDay), tostring(gt.hour), tostring(gt.minute))
    end
    local okC, clubs = pcall(C_Club.GetSubscribedClubs)
    if okC and type(clubs) == "table" then
        L[#L + 1] = ("clubs : %d"):format(#clubs)
        for i, c in ipairs(clubs) do
            if i <= 5 then
                L[#L + 1] = ("  club %d : id=%s type=%s nom=%s"):format(i, tostring(c.clubId),
                    tostring(c.clubType), tostring(c.name))
            end
        end
    end
    local okG, gid = pcall(C_Club.GetGuildClubId)
    L[#L + 1] = "guildClubId : " .. tostring(okG and gid or "erreur")
    local ok1, n1 = pcall(C_Calendar.GetNumGuildEvents)
    L[#L + 1] = "GetNumGuildEvents : " .. tostring(ok1 and n1 or "erreur")
    if okG and gid then
        local ct0, ct1
        local okT, ct = pcall(C_DateAndTime.GetCurrentCalendarTime)
        if okT then
            ct0 = ct
            local okT2, ct2 = pcall(C_DateAndTime.AdjustTimeByDays, ct, WINDOW_DAYS)
            ct1 = okT2 and ct2 or nil
        end
        if ct0 and ct1 then
            local ok2, evs = pcall(C_Calendar.GetClubCalendarEvents, gid, ct0, ct1)
            L[#L + 1] = "GetClubCalendarEvents : " .. tostring(ok2 and (type(evs) == "table" and #evs or "?") or "erreur")
            if ok2 and type(evs) == "table" then
                for i = 1, math.min(2, #evs) do
                    local e = evs[i]
                    local d = timeFields(e.startTime)
                    L[#L + 1] = ("  #%d %s | %s | id=%s"):format(i, tostring(e.title), tostring(d), tostring(e.eventID))
                end
            end
        else
            L[#L + 1] = "GetClubCalendarEvents : dates indisponibles"
        end
    end
    -- état du mois affiché + balayage jour par jour (offsets 0 à 2, lecture seule)
    local okM, mi = pcall(C_Calendar.GetMonthInfo, 0)
    if okM and type(mi) == "table" then
        L[#L + 1] = ("mois affiché : %s/%s (%s jours)"):format(tostring(mi.month), tostring(mi.year), tostring(mi.numDays))
    end
    for off = 0, 2 do
        local total = 0
        local samples = {}
        local numDays = 0
        local okMo, mio = pcall(C_Calendar.GetMonthInfo, off)
        if okMo and type(mio) == "table" then numDays = tonumber(mio.numDays) or 31 end
        for day = 1, numDays do
            local okN, n = pcall(C_Calendar.GetNumDayEvents, off, day)
            n = tonumber(okN and n or 0) or 0
            total = total + n
            for i = 1, n do
                if #samples < 3 then
                    local okE, e = pcall(C_Calendar.GetDayEvent, off, day, i)
                    if okE and type(e) == "table" then
                        local d = timeFields(e.startTime)
                        samples[#samples + 1] = ("j%s #%s [%s] %s (%s)"):format(tostring(day), tostring(i),
                            tostring(e.calendarType), tostring(e.title), tostring(d))
                    end
                end
            end
        end
        L[#L + 1] = ("offset %d : %d événement(s) au total"):format(off, total)
        for _, s in ipairs(samples) do L[#L + 1] = "   " .. s end
    end
    local ok3, ni = pcall(C_Calendar.GetNumInvites)
    L[#L + 1] = "GetNumInvites (événement ouvert) : " .. tostring(ok3 and ni or "erreur")
    return L
end

local function dumpDiag(writeFile)
    local L = diagLines()
    for _, line in ipairs(L) do
        msg(line)
    end
    if writeFile then
        LOTP_DB.diag = table.concat(L, "\n")
        LOTP_DB.diag_at = time()
        msg("rapport enregistré — tape /reload puis envoie le fichier "
            .. "WTF/Account/<compte>/SavedVariables/LOTP.lua")
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
mkButton("Diag → fichier", 236, 130, function()
    dumpDiag(true)
end)
mkButton("Fermer", 374, 100, function() ui:Hide() end)

SLASH_LOTP1 = "/lotp"
SlashCmdList["LOTP"] = function(arg)
    arg = (arg or ""):lower()
    if arg == "" then
        ui:Show()
        if LOTP_DB.export then pcall(showSummary) end
        -- toujours relancer une collecte (sauf si une vient de finir il y a < 2 min)
        if not collecting and (time() - (LOTP_DB.export_at or 0)) > 120 then
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
        dumpDiag(true)
    else
        msg("commandes : /lotp · /lotp collect · /lotp export · /lotp diag")
    end
end
