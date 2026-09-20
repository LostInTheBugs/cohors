-- Cohors — Calendrier de guilde → le site de la guilde
-- Collecte les événements de guilde (raids, invitations, réponses) et génère
-- une chaîne à coller sur le site (page Calendrier → « Importer »).
-- Commandes : /cohors · /cohors collect · /cohors export · /cohors diag · /cohors reset
--
-- Lecture du calendrier : même méthode que l'UI Blizzard — on affiche le mois
-- (SetAbsMonth/SetMonth) puis on lit les jours (GetNumDayEvents/GetDayEvent),
-- et on ouvre chaque événement (OpenEvent(0, jour, index)) pour les réponses.
-- Le moteur avance image par image (OnUpdate), jamais par minuteurs : même si
-- une étape échoue, la collecte se termine et écrit son rapport.
local ADDON_NAME = ...
local ADDON_VER = "1.7.0"
local WINDOW_DAYS = 21
local MAX_EVENTS = 40
local MONTH_WAIT = 1.0          -- attente de chargement avant lecture d'un mois
local OPEN_WAIT = 0.5           -- attente après positionnement avant OpenEvent
local EVENT_TIMEOUT = 3.0       -- attente maximale de CALENDAR_OPEN_EVENT
local GLOBAL_TIMEOUT = 90       -- durée maximale d'une collecte

Cohors_DB = Cohors_DB or {}

local f = CreateFrame("Frame")
f:Show()

local collecting = false
local engine = nil        -- machine à états pilotée par OnUpdate
local results = {}
local openedFrame = false
local watchMonth = nil
local collectStartAt = 0
local ui, statusText, eb, showSummary  -- créés plus bas (panneau à la demande)

-- ---------------------------------------------------------------- utilitaires
local function msg(text)
    DEFAULT_CHAT_FRAME:AddMessage("|cffdfa55aCohors|r " .. tostring(text))
end

local function clientIface()
    local ok, _, _, _, iface = pcall(GetBuildInfo)
    if not ok or type(iface) ~= "number" then return "?" end
    return iface
end

local function tocVersion()
    local ok, v = pcall(function()
        if C_AddOns and C_AddOns.GetAddOnMetadata then
            return C_AddOns.GetAddOnMetadata(ADDON_NAME or "Cohors", "Version")
        end
        return GetAddOnMetadata and GetAddOnMetadata(ADDON_NAME or "Cohors", "Version")
    end)
    return (ok and v and tostring(v)) or "?"
end

local function jsonEsc(s)
    s = tostring(s or "")
    s = s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t")
    return s
end

-- nombre JSON sûr : le client WoW écrit certains ids 64 bits en hexadécimal (0x1F45…) — invalide en JSON strict
local function jsonNum(v)
    local s = tostring(v == nil and 0 or v)
    if s:match("^%-?%d+$") then return s end
    return '"' .. jsonEsc(s) .. '"'
end

local function dateStr(t)
    local ok, s = pcall(date, "%Y-%m-%d %H:%M", t)  -- date() natif du client WoW (os absent en jeu)
    return ok and s or "?"
end

-- conversion (annee, mois, jour, h, min) -> epoch sans os.time (absent du client WoW) :
-- algorithme jours-depuis-civil + decalage par rapport a l'heure courante
local function toEpoch(f)
    if not (f and f.year and f.month and (f.monthDay or f.day)) then return 0 end
    local nowT = date("*t", time())
    local function daysFromCivil(y, m, d)
        y = (m <= 2) and (y - 1) or y
        local era = math.floor(y / 400)
        local yoe = y - era * 400
        local mp = (m + 9) % 12
        local doy = math.floor((153 * mp + 2) / 5) + d - 1
        local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
        return era * 146097 + doe - 719468
    end
    local dNow = daysFromCivil(nowT.year, nowT.month, nowT.day)
    local dEv = daysFromCivil(f.year, f.month, f.monthDay or f.day)
    local secNow = (nowT.hour or 0) * 3600 + (nowT.min or 0) * 60 + (nowT.sec or 0)
    local secEv = (f.hour or 0) * 3600 + (f.minute or 0) * 60
    return time() - secNow + (dEv - dNow) * 86400 + secEv
end

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
    local ok, t = pcall(toEpoch, { year = y, month = mo, monthDay = d, hour = h, minute = mi })
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

local function dtrace(s)
    Cohors_DB.trace = tostring(Cohors_DB.trace or ("trace — " .. dateStr(time()))) .. "\n- " .. tostring(s)
end

local function positionView(shift)
    local now = nowCalendarTime()
    pcall(C_Calendar.SetAbsMonth, now.month, now.year)
    if (shift or 0) > 0 then
        pcall(C_Calendar.SetMonth, shift)
    end
end

local function recordEvent(ev, invs)
    results[#results + 1] = { id = ev.id, title = ev.title, date = ev.date, ts = ev.ts,
                              etype = ev.etype, invites = invs or {} }
end

-- liste des dossiers d'addon « Cohors* » chargés (détection de doublons)
local function cohorsCopies()
    local out = {}
    local ok, n = pcall(function()
        if C_AddOns and C_AddOns.GetNumAddOns then return C_AddOns.GetNumAddOns() end
        return GetNumAddOns and GetNumAddOns() or 0
    end)
    if not ok or type(n) ~= "number" then return out end
    for i = 1, n do
        local okn, name = pcall(function()
            if C_AddOns and C_AddOns.GetAddOnInfo then return select(1, C_AddOns.GetAddOnInfo(i)) end
            return select(1, GetAddOnInfo(i))
        end)
        if okn and type(name) == "string" and name:upper():find("^Cohors") then
            local okv, ver = pcall(function()
                if C_AddOns and C_AddOns.GetAddOnMetadata then return C_AddOns.GetAddOnMetadata(name, "Version") end
                return GetAddOnMetadata(name, "Version")
            end)
            out[#out + 1] = name .. " v" .. tostring(okv and ver or "?")
        end
    end
    return out
end

local diagLines    -- définies plus bas
local finishCollect
local recTickSafe   -- moteur recettes (défini plus bas)
local progressUpdate  -- fenêtre de progression (définie plus bas)

-- ------------------------------------------------------------------- export
local function buildExport()
    local parts = {}
    parts[#parts + 1] = '{"v":1,"ver":"' .. jsonEsc(ADDON_VER) .. '"'
    parts[#parts + 1] = ',"player":"' .. jsonEsc(UnitName("player") or "?") .. '"'
    parts[#parts + 1] = ',"realm":"' .. jsonEsc(GetRealmName() or "?") .. '"'
    parts[#parts + 1] = ',"at":' .. jsonNum(time()) .. ',"events":['
    for i, e in ipairs(results) do
        local ev = {}
        ev[#ev + 1] = '{"id":' .. jsonNum(e.id)
        ev[#ev + 1] = ',"title":"' .. jsonEsc(e.title) .. '"'
        ev[#ev + 1] = ',"date":"' .. jsonEsc(e.date) .. '"'
        ev[#ev + 1] = ',"ts":' .. jsonNum(e.ts)
        ev[#ev + 1] = ',"type":' .. jsonNum(e.etype)
        ev[#ev + 1] = ',"inv":['
        for j, inv in ipairs(e.invites or {}) do
            if j > 1 then ev[#ev + 1] = "," end
            ev[#ev + 1] = '{"n":"' .. jsonEsc(inv.n) .. '","s":' .. jsonNum(inv.s or -1)
            if inv.t then ev[#ev + 1] = ',"t":' .. jsonNum(inv.t) end
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
    local n = tonumber(ok and num or 0) or 0
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
    engine = nil
    restoreCalendar()
    Cohors_DB.export = buildExport()
    Cohors_DB.export_at = time()
    Cohors_DB.player = UnitName("player")
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
        lines[#lines + 1] = tostring(Cohors_DB.trace or "?")
        Cohors_DB.diag = table.concat(lines, "\n")
        Cohors_DB.diag_at = time()
    end
    local nresp = 0
    for _, e in ipairs(results) do
        nresp = nresp + #(e.invites or {})
    end
    if #results == 0 then
        msg("aucun événement trouvé. Ouvre le calendrier du jeu (touche C) pour vérifier, puis /cohors. "
            .. "(rapport enregistré : /reload puis envoie le fichier Cohors.lua)")
    else
        msg(("%d raid(s) collecté(s), %d réponse(s). • /cohors export pour la chaîne à coller sur le site.")
            :format(#results, nresp))
    end
    progressUpdate(("calendrier — terminé : %d raid(s), %d réponse(s)"):format(#results, nresp), 1, true)
    if Cohors_Refresh then Cohors_Refresh() end
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

-- ------------------------------------------- fenêtre de progression (exports)
-- Petite fenêtre autonome, construite à la demande, qui montre l'avancement
-- des exports (recettes, calendrier) : barre + pourcentage + détail vivant.
-- Elle s'affiche même quand le panneau principal est fermé, se laisse déplacer,
-- et disparaît quelques secondes après la fin. Toute construction ratée est
-- sans conséquence (l'addon continue, l'erreur est notée dans le rapport).
local pWin, pBar, pBarText, pLabel, pHideAt

local function buildProgress()
    if pWin then return true end
    local okB, errB = pcall(function()
        local okF, frame = pcall(CreateFrame, "Frame", "CohorsProgressFrame", UIParent,
            BackdropTemplateMixin and "BackdropTemplate" or nil)
        if not okF or not frame then
            frame = CreateFrame("Frame", nil, UIParent, BackdropTemplateMixin and "BackdropTemplate" or nil)
        end
        pWin = frame
        pWin:SetSize(440, 78)
        pWin:SetPoint("TOP", UIParent, "TOP", 0, -170)
        pWin:SetFrameStrata("HIGH")
        pWin:SetMovable(true)
        pWin:EnableMouse(true)
        pWin:RegisterForDrag("LeftButton")
        pWin:SetScript("OnDragStart", pWin.StartMoving)
        pWin:SetScript("OnDragStop", pWin.StopMovingOrSizing)
        if pWin.SetBackdrop then
            pWin:SetBackdrop({
                bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
                edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
                tile = true, tileSize = 16, edgeSize = 24,
                insets = { left = 4, right = 4, top = 4, bottom = 4 },
            })
        end
        local title = pWin:CreateFontString(nil, "OVERLAY", "GameFontNormal")
        title:SetPoint("TOP", 0, -8)
        title:SetText("Cohors — progression")
        pLabel = pWin:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
        pLabel:SetPoint("TOP", 0, -26)
        pLabel:SetWidth(408)
        pLabel:SetJustifyH("CENTER")
        pLabel:SetText("…")
        pBar = CreateFrame("StatusBar", nil, pWin)
        pBar:SetSize(400, 16)
        pBar:SetPoint("TOP", 0, -52)
        pBar:SetStatusBarTexture("Interface\\TargetingFrame\\UI-StatusBar")
        pBar:SetMinMaxValues(0, 100)
        pBar:SetValue(0)
        if pBar.SetStatusBarColor then pBar:SetStatusBarColor(0.69, 0.0, 0.18, 1) end
        pBarText = pBar:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        pBarText:SetPoint("CENTER")
        pBarText:SetText("0 %")
        pWin:Hide()
        pWin:SetScript("OnUpdate", function()
            if pHideAt and GetTime() >= pHideAt then
                pHideAt = nil
                pWin:Hide()
            end
        end)
    end)
    if not okB then
        Cohors_DB.ui_error = "progression : " .. tostring(errB)
        pWin = nil
        return false
    end
    return true
end

-- progressUpdate(label, pct 0..1, done) — met à jour la ligne de statut du panneau
-- et la petite fenêtre ; done=true affiche 100 %, coche et referme après 8 s.
progressUpdate = function(label, pct, done)
    if statusText then statusText:SetText(tostring(label or "prêt")) end
    if not buildProgress() or not pWin then return end
    pWin:Show()
    if done then
        pBar:SetValue(100)
        pBarText:SetText("\226\156\148")  -- ✔ (UTF-8)
        pHideAt = GetTime() + 8
    else
        pHideAt = nil
        local v = math.floor(math.max(0, math.min(1, pct or 0)) * 100 + 0.5)
        pBar:SetValue(v)
        pBarText:SetText(v .. " %")
    end
end

-- ------------------------------------------------- moteur piloté par OnUpdate
local function engineTick(now, force)
    local e = engine
    if not e then return end
    if now > e.deadline then
        dtrace("délai global dépassé")
        msg("collecte interrompue (délai global) — le rapport a été enregistré.")
        finishCollect()
        return
    end
    local ph = e.phase
    if ph == "scanPos" then
        if not force and now < e.await then return end
        positionView(e.shift)
        e.phase = "scanRead"
        e.await = now + (e.shift == 0 and 0.6 or MONTH_WAIT)
        e.status = ("balayage du mois +%d…"):format(e.shift)
        return
    elseif ph == "scanRead" then
        if not force and now < e.await then return end
        local found = scanViewedMonth(e.shift)
        dtrace(("mois +%d : %d événement(s)"):format(e.shift, #found))
        msg(("• mois +%d : %d événement(s)"):format(e.shift, #found))
        for _, ev in ipairs(found) do e.scanned[#e.scanned + 1] = ev end
        e.shift = e.shift + 1
        if e.shift <= e.maxShift then
            e.phase = "scanPos"
            e.await = 0
        else
            local list = filterWindow(e.scanned)
            if #list > 0 then
                e.queue = list
                e.qi = 1
                e.phase = "openPos"
                e.await = 0
                dtrace(("%d événement(s) à ouvrir"):format(#list))
                msg(("%d événement(s) à collecter…"):format(#list))
            elseif not e.rescan then
                e.rescan = true
                e.phase = "rescanPos"
                e.await = now + 2.0
                e.status = "nouvelle tentative…"
            else
                finishCollect()
            end
        end
        return
    elseif ph == "rescanPos" then
        if not force and now < e.await then return end
        e.scanned = {}
        e.shift = 0
        e.phase = "scanPos"
        e.await = 0
        return
    elseif ph == "openPos" then
        if e.qi > #e.queue then
            dtrace("ouvertures terminées")
            finishCollect()
            return
        end
        if not force and now < e.await then return end
        local ev = e.queue[e.qi]
        e.current = ev
        positionView(ev.shift)
        e.phase = "openFire"
        e.await = now + OPEN_WAIT
        e.status = ("ouverture %d/%d…"):format(e.qi, #e.queue)
        return
    elseif ph == "openFire" then
        if not force and now < e.await then return end
        local ev = e.current
        dtrace(("ouverture #%d : %s (%s)"):format(e.qi, tostring(ev.title), tostring(ev.date)))
        e.eventAt = nil
        local okc, opened = pcall(C_Calendar.OpenEvent, 0, ev.day, ev.idx)
        if (not okc) or opened == false then
            dtrace("  → ouverture impossible")
            recordEvent(ev, {})
            e.current = nil
            e.qi = e.qi + 1
            e.phase = "openPos"
            e.await = now + 0.1
            return
        end
        e.phase = "openWait"
        e.await = now + EVENT_TIMEOUT
        return
    elseif ph == "openWait" then
        local ev = e.current
        if (e.eventAt and now >= e.eventAt + 0.3) or force then
            local invs = readInvites()
            dtrace(("  → %d réponse(s)"):format(#invs))
            msg(("• %s — %d réponse(s)"):format(tostring(ev.title), #invs))
            recordEvent(ev, invs)
            pcall(C_Calendar.CloseEvent)
            e.current = nil
            e.qi = e.qi + 1
            e.phase = "openPos"
            e.await = now + 0.2
            return
        end
        if now >= e.await then
            dtrace("  → pas de réponse du calendrier (délai)")
            recordEvent(ev, {})
            e.current = nil
            e.qi = e.qi + 1
            e.phase = "openPos"
            e.await = now + 0.2
        end
        return
    end
end

local function engineTickSafe(force)
    if not engine then return end
    local ok, err = pcall(engineTick, GetTime(), force)
    if not ok then
        local t = tostring(err)
        Cohors_DB.last_error = "moteur : " .. t
        dtrace("ERREUR moteur : " .. t)
        msg("erreur (moteur) — " .. t)
        pcall(finishCollect)
    end
    if engine then
        local e = engine
        local pct, step
        if e.phase == "openPos" or e.phase == "openFire" or e.phase == "openWait" then
            local nq = #(e.queue or {})
            pct = 0.5 + 0.5 * (((e.qi or 1) - 1) / math.max(1, nq))
            step = ("raids %d/%d"):format(math.min(e.qi or 1, math.max(1, nq)), math.max(1, nq))
        else
            local nm = (e.maxShift or 0) + 1
            pct = ((e.shift or 0) / math.max(1, nm)) * 0.5
            step = ("mois %d/%d"):format(math.min((e.shift or 0) + 1, nm), nm)
        end
        progressUpdate(("calendrier — %s · %s"):format(step, tostring(e.status or "")), pct)
    end
end

f:SetScript("OnUpdate", function()
    engineTickSafe()
    recTickSafe()
end)
-- pas de minuteur créé au chargement : la collecte avance par l'affichage (OnUpdate) et par les clics
-- (chaque clic sur « Collecter » force une étape, même si l'affichage ne tourne pas)
f:SetScript("OnEvent", function(_, event, arg1)
    if event == "ADDON_LOADED" then
        if arg1 == (ADDON_NAME or "Cohors") then
            Cohors_DB.loaded_ver = ADDON_VER
            Cohors_DB.loaded_at = time()
            Cohors_DB.loaded_dossier = tostring(ADDON_NAME or "?")
            local copies = cohorsCopies()
            if #copies > 1 then
                msg("|cffff5555ATTENTION : plusieurs dossiers Cohors détectés (" ..
                    table.concat(copies, ", ") .. ") — supprime les doublons !|r")
            end
            msg(("v%s chargée (client %s · dossier « %s ») — %s"):format(ADDON_VER, tostring(clientIface()),
                tostring(ADDON_NAME or "?"),
                Cohors_DB.export and ("dernier export : " .. dateStr(Cohors_DB.export_at or 0) ..
                    " • /cohors pour ouvrir") or "/cohors pour collecter le calendrier de guilde"))
        end
    elseif event == "CALENDAR_OPEN_EVENT" then
        if engine and engine.current then
            engine.eventAt = GetTime()
        end
    end
end)
f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("CALENDAR_OPEN_EVENT")

function Cohors_Collect()
    if engine then
        local age = GetTime() - (engine.startedAt or 0)
        if age > 60 then
            msg(("collecte précédente bloquée (%d s) — réinitialisation…"):format(math.floor(age)))
            dtrace("réinitialisation forcée")
            engine = nil
            collecting = false
        else
            dtrace("clic : étape forcée (phase " .. tostring(engine.phase) .. ")")
            engineTickSafe(true)
            if engine then
                msg("étape forcée (« " .. tostring(engine.phase) .. " ») — reclique « Collecter » pour continuer.")
            else
                msg("étape forcée — collecte terminée.")
            end
            return
        end
    end
    collecting = true
    results = {}
    collectStartAt = time()
    Cohors_DB.last_error = nil
    Cohors_DB.trace = ("trace — %s (addon v%s · client %s · dossier « %s »)"):format(dateStr(time()),
        ADDON_VER, tostring(clientIface()), tostring(ADDON_NAME or "?"))
    dtrace("collecte démarrée")
    local now = GetTime()
    engine = {
        startedAt = now, deadline = now + GLOBAL_TIMEOUT,
        phase = "scanPos", shift = 0, maxShift = 2, await = now + 0.5,
        scanned = {}, queue = {}, qi = 1, rescan = false, status = "préparation…",
    }
    local cnow = nowCalendarTime()
    watchMonth = { month = cnow.month, year = cnow.year }
    openCalendarFrame()
    pcall(C_Calendar.OpenCalendar)
    pcall(C_Calendar.SetAbsMonth, cnow.month, cnow.year)
    local okA, ctEnd = pcall(C_DateAndTime.GetCalendarTimeFromEpoch, time() + WINDOW_DAYS * 86400)
    if okA and type(ctEnd) == "table" and ctEnd.month and ctEnd.year then
        engine.maxShift = math.max(0, math.min(3, (ctEnd.year * 12 + ctEnd.month) - (cnow.year * 12 + cnow.month)))
    end
    dtrace(("vue : mois +0 à +%d"):format(engine.maxShift))
    msg("lecture du calendrier de guilde…")
    progressUpdate("calendrier — préparation…", 0)
end

function Cohors_Reset()
    if engine or collecting then
        engine = nil
        collecting = false
        pcall(restoreCalendar)
        if pWin then pHideAt = nil; pWin:Hide() end
        if statusText then statusText:SetText("prêt") end
        msg("collecte réinitialisée — tu peux relancer.")
    else
        msg("rien à réinitialiser.")
    end
end

-- ------------------------------------------------------------------ diagnostic
diagLines = function()
    local L = {}
    L[#L + 1] = "Cohors diag — " .. dateStr(time())
    L[#L + 1] = ("addon v%s (TOC %s) · client %s · dossier « %s »"):format(ADDON_VER, tostring(tocVersion()),
        tostring(clientIface()), tostring(ADDON_NAME or "?"))
    if Cohors_DB.ui_error then
        L[#L + 1] = "erreur fenêtre : " .. tostring(Cohors_DB.ui_error)
    end
    L[#L + 1] = ("collecte : %s"):format(collecting and
        ("en cours depuis " .. dateStr(collectStartAt) .. " · phase " .. tostring(engine and engine.phase)) or "au repos")
    local copies = cohorsCopies()
    L[#L + 1] = "dossiers Cohors : " .. (#copies > 0 and table.concat(copies, ", ") or "?")
    if Cohors_DB.last_error then
        L[#L + 1] = "dernière erreur : " .. tostring(Cohors_DB.last_error)
    end
    L[#L + 1] = ("joueur %s — %s"):format(tostring(UnitName("player")), tostring(GetRealmName()))
    local okS, shown = pcall(function() return CalendarFrame and (CalendarFrame:IsShown() and "affiché" or "existe (fermé)") end)
    L[#L + 1] = "CalendarFrame : " .. tostring(okS and shown or "absent")
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
        local okT, ct = pcall(C_DateAndTime.GetCurrentCalendarTime)
        local okT2, ct2 = pcall(C_DateAndTime.AdjustTimeByDays, okT and ct or nil, WINDOW_DAYS)
        if okT and okT2 and type(ct) == "table" and type(ct2) == "table" then
            local ok2, evs = pcall(C_Calendar.GetClubCalendarEvents, gid, ct, ct2)
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
    L[#L + 1] = ("recettes exportées : %s (total %s)"):format(
        Cohors_DB.recipes and (dateStr(Cohors_DB.recipes_at or 0)) or "aucune", tostring(Cohors_DB.recipes_total or 0))
    if engine then
        L[#L + 1] = "collecte calendrier en cours : phase " .. tostring(engine.phase)
    end
    if recEngine then
        L[#L + 1] = ("export recettes en cours : métier %d/%d, phase %s"):format(
            recEngine.pi or 0, (recEngine.progs and #recEngine.progs) or 0, tostring(recEngine.phase))
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
        Cohors_DB.diag = table.concat(L, "\n")
        Cohors_DB.diag_at = time()
        msg("rapport enregistré — tape /reload puis envoie le fichier "
            .. "WTF/Account/<compte>/SavedVariables/Cohors.lua")
    end
end

-- ------------------------------------------------- recettes des artisans (export)
-- Lit les recettes connues du personnage, PAR MÉTIER ET PAR EXTENSION
-- (paliers GetChildProfessionInfos, comme l'interface des métiers), et les
-- écrit dans Cohors_DB.recipes pour l'import sur le site (Préparation de raid).
local recEngine = nil
local recResults = {}
local REC_TOTAL_TIMEOUT = 240

local function itemName(id)
    if not id or id <= 0 then return "" end
    local ok, name = pcall(function()
        return C_Item.GetItemNameByID(id) or select(1, C_Item.GetItemInfo(id))
    end)
    if ok and type(name) == "string" and name ~= "" then return name end
    return ""
end

local function recProfs()
    local out = {}
    local ok, p1, p2, p3, p4, p5 = pcall(GetProfessions)
    if not ok then return out end
    for _, idx in ipairs({ p1, p2, p3, p4, p5 }) do
        if idx then
            -- GetProfessionInfo(index) : name, icon, lvl, max, nb, offset, skillLine, ...
            local info = { pcall(GetProfessionInfo, idx) }
            local name, skillLine = info[2], info[8]
            if info[1] and name and tonumber(skillLine) and tonumber(skillLine) > 0 then
                out[#out + 1] = { name = tostring(name), skillLine = skillLine }
            end
        end
    end
    return out
end

local function collectProfession(eName, eRank)
    local out = {}
    local seen = {}
    local ok, ids = pcall(C_TradeSkillUI.GetFilteredRecipeIDs)
    if not ok or type(ids) ~= "table" then return out end
    for _, rid in ipairs(ids) do
        local oki, info = pcall(C_TradeSkillUI.GetRecipeInfo, rid)
        if oki and type(info) == "table" and info.name and not seen[info.name] then
            seen[info.name] = true
            local mats = {}
            local oks, sch = pcall(C_TradeSkillUI.GetRecipeSchematic, rid, false)
            if oks and type(sch) == "table" then
                for _, slot in ipairs(sch.reagentSlotSchematics or {}) do
                    local qty = tonumber(slot.quantityRequired) or 0
                    for _, r in ipairs(slot.reagents or {}) do
                        local iid = tonumber(r.itemID) or 0
                        if iid > 0 then
                            mats[#mats + 1] = { iid, itemName(iid), qty }
                            break
                        end
                    end
                end
            end
            out[#out + 1] = { n = tostring(info.name), i = info.recipeID or rid,
                              e = eName or "", t = eRank or 0, m = mats }
        end
    end
    return out
end

local function recSave()
    local parts = {}
    parts[#parts + 1] = '{"v":1,"player":"' .. jsonEsc(UnitName("player") or "?") .. '"'
    parts[#parts + 1] = ',"realm":"' .. jsonEsc(GetRealmName() or "?") .. '"'
    parts[#parts + 1] = ',"at":' .. jsonNum(time()) .. ',"professions":['
    local firstP = true
    for _, prof in ipairs(recResults) do
        if not firstP then parts[#parts + 1] = "," end
        firstP = false
        parts[#parts + 1] = '{"name":"' .. jsonEsc(prof.name) .. '","recipes":['
        local firstR = true
        for _, r in ipairs(prof.recipes or {}) do
            if not firstR then parts[#parts + 1] = "," end
            firstR = false
            parts[#parts + 1] = '{"n":"' .. jsonEsc(r.n) .. '","i":' .. jsonNum(r.i)
                .. ',"e":"' .. jsonEsc(r.e or "") .. '","t":' .. jsonNum(r.t or 0) .. ',"m":['
            local firstM = true
            for _, m in ipairs(r.m or {}) do
                if not firstM then parts[#parts + 1] = "," end
                firstM = false
                parts[#parts + 1] = "[" .. jsonNum(m[1]) .. ',"' .. jsonEsc(m[2]) .. '",' .. jsonNum(m[3]) .. "]"
            end
            parts[#parts + 1] = "]}"
        end
        parts[#parts + 1] = "]}"
    end
    parts[#parts + 1] = "]}"
    Cohors_DB.recipes = table.concat(parts)
    Cohors_DB.recipes_at = time()
end

local function recFinish(note)
    if not recEngine then return end
    recEngine = nil
    pcall(C_TradeSkillUI.CloseTradeSkill)
    pcall(recSave)
    local total, exts = 0, {}
    for _, prof in ipairs(recResults) do
        for _, r in ipairs(prof.recipes or {}) do
            total = total + 1
            if r.e and r.e ~= "" and not exts[r.e] then exts[r.e] = true end
        end
    end
    Cohors_DB.recipes_total = total
    msg(("%d recette(s) exportée(s)%s — tape /reload PUIS envoie le fichier WTF/Account/<compte>/SavedVariables/Cohors.lua au site (Préparation de raid → 📥 importer).")
        :format(total, note and (" (" .. tostring(note) .. ")") or ""))
    progressUpdate(("recettes — terminé : %d recette(s)"):format(total), 1, true)
end

recTick = function(now, force)
    local e = recEngine
    if not e then return end
    if now > e.deadline then
        dtrace("recettes : délai global dépassé")
        recFinish("délai dépassé")
        return
    end
    local prof = e.progs[e.pi]
    if not prof then
        dtrace("recettes : terminé")
        recFinish(nil)
        return
    end
    local wait = (not force) and now < (e.await or 0)

    if e.phase == "open" then
        if wait then return end
        pcall(C_TradeSkillUI.OpenTradeSkill, prof.skillLine)
        e.phase = "ready"; e.attempts = 0; e.await = now + 0.9
        dtrace(("métier %d/%d : %s"):format(e.pi, #e.progs, prof.name))
        return
    elseif e.phase == "ready" then
        if wait then return end
        local oki, ready = pcall(C_TradeSkillUI.IsTradeSkillReady)
        local okc, childs = pcall(C_TradeSkillUI.GetChildProfessionInfos)
        if (oki and ready) or (e.attempts or 0) > 12 then
            e.childs = (okc and type(childs) == "table" and #childs > 0) and childs or {}
            e.ci = 1
            if #e.childs > 0 then
                e.phase = "switch"; e.await = now + 0.2
            else
                e.phase = "waitList"; e.attempts = 0; e.await = now + 1.0
            end
            return
        end
        e.attempts = (e.attempts or 0) + 1; e.await = now + 0.6
        return
    elseif e.phase == "switch" then
        if wait then return end
        local child = e.childs[e.ci]
        if not child then
            pcall(C_TradeSkillUI.CloseTradeSkill)
            e.pi = e.pi + 1; e.ci = 0; e.childs = nil
            e.phase = "open"; e.await = now + 0.5
            return
        end
        pcall(C_TradeSkillUI.SetProfessionChildSkillLineID, child.professionID)
        e.attempts = 0; e.phase = "waitChild"; e.await = now + 0.4
        return
    elseif e.phase == "waitChild" then
        if wait then return end
        local child = e.childs[e.ci]
        local okg, cur = pcall(C_TradeSkillUI.GetChildProfessionInfo)
        if okg and type(cur) == "table" and cur.professionID == child.professionID then
            e.phase = "settle"; e.settleUntil = now + 0.9
        elseif (e.attempts or 0) > 10 then
            dtrace(("%s · %s : palier non chargé — passé"):format(prof.name, tostring(child.expansionName)))
            e.ci = e.ci + 1; e.phase = "switch"; e.await = now + 0.2
        else
            e.attempts = (e.attempts or 0) + 1; e.await = now + 0.4
        end
        return
    elseif e.phase == "settle" then
        if not force and now < (e.settleUntil or 0) then return end
        e.phase = "collect"; e.await = now + 0.05
        return
    elseif e.phase == "collect" then
        if wait then return end
        local child = e.childs[e.ci]
        local eName, eRank = "", 0
        if child then
            eName = tostring(child.expansionName or "")
            eRank = e.ci
        end
        local okc2, list = pcall(collectProfession, eName, eRank)
        list = (okc2 and type(list) == "table") and list or {}
        recResults[#recResults + 1] = { name = prof.name, recipes = list }
        local tag = eName ~= "" and (" · " .. eName) or ""
        dtrace(("%s%s : %d recette(s)"):format(prof.name, tag, #list))
        msg(("• %s%s : %d recette(s)"):format(prof.name, tag, #list))
        if child then
            e.ci = e.ci + 1; e.phase = "switch"; e.await = now + 0.3
        else
            pcall(C_TradeSkillUI.CloseTradeSkill)
            e.pi = e.pi + 1; e.ci = 0; e.childs = nil
            e.phase = "open"; e.await = now + 0.5
        end
        return
    elseif e.phase == "waitList" then
        if wait then return end
        local okL, ids = pcall(C_TradeSkillUI.GetFilteredRecipeIDs)
        if okL and type(ids) == "table" and #ids > 0 then
            e.phase = "collect"; e.await = now + 0.05
            return
        end
        e.attempts = (e.attempts or 0) + 1
        if e.attempts > 20 then
            dtrace(("%s : aucune recette lue — métier suivant"):format(prof.name))
            pcall(C_TradeSkillUI.CloseTradeSkill)
            e.pi = e.pi + 1; e.ci = 0; e.childs = nil
            e.phase = "open"; e.await = now + 0.4
            return
        end
        e.await = now + 1.0
        return
    end
end

recTickSafe = function(force)
    if not recEngine then return end
    local ok, err = pcall(recTick, GetTime(), force)
    if not ok then
        local t2 = tostring(err)
        Cohors_DB.last_error = "recettes : " .. t2
        dtrace("ERREUR recettes : " .. t2)
        msg("erreur (recettes) — " .. t2)
        pcall(recFinish, "erreur")
    end
    if recEngine then
        local e = recEngine
        local pname = (e.progs and e.progs[e.pi] and e.progs[e.pi].name) or "?"
        local nchild = (e.childs and #e.childs) or 0
        local ci = tonumber(e.ci) or 0
        local frac = 0
        if nchild > 0 and ci > 0 then
            frac = math.min(1, (ci - 1) / nchild)   -- palier suivant le dernier = métier terminé
        end
        local pct = ((e.pi - 1) + frac) / math.max(1, (e.progs and #e.progs) or 1)
        local n = 0
        for _, prof in ipairs(recResults) do n = n + #(prof.recipes or {}) end
        local sub
        if e.phase == "open" or e.phase == "ready" then
            sub = "ouverture de la fenêtre de métier…"
        elseif (e.phase == "switch" or e.phase == "waitChild" or e.phase == "settle") and nchild > 0 then
            local child = e.childs[ci]
            sub = ("palier %d/%d"):format(ci, nchild)
                .. (child and child.expansionName and (" · " .. tostring(child.expansionName)) or "")
        elseif e.phase == "collect" then
            sub = "lecture des recettes…"
        elseif e.phase == "waitList" then
            sub = "attente de la liste des recettes…"
        else
            sub = tostring(e.phase)
        end
        progressUpdate(("recettes — %s : %s · %d recette(s) · %d s"):format(
            pname, sub, n, math.floor(GetTime() - (e.started or GetTime()))), pct)
    end
end

function Cohors_Recipes()
    if recEngine then
        local age = GetTime() - (recEngine.started or 0)
        if age > 150 then
            msg("export précédent bloqué — réinitialisation…")
            recEngine = nil
        else
            dtrace("clic : étape recettes forcée")
            recTickSafe(true)
            msg("export en cours — étape forcée (" .. tostring(recEngine and recEngine.phase) .. ").")
            return
        end
    end
    local profs = recProfs()
    Cohors_DB.rec_trace = ("recettes — %s (addon v%s · client %s)"):format(dateStr(time()), ADDON_VER,
        tostring(clientIface()))
    if #profs == 0 then
        msg("aucun métier détecté sur ce personnage.")
        return
    end
    recResults = {}
    recEngine = {
        progs = profs, pi = 1, phase = "open", attempts = 0, ci = 0,
        started = GetTime(), await = GetTime() + 0.4, settleUntil = 0,
        deadline = GetTime() + REC_TOTAL_TIMEOUT,
    }
    dtrace(("recettes : %d métier(s) — %s"):format(#profs, profs[1] and profs[1].name or "?"))
    msg(("lecture des recettes (%d métier(s), tous paliers d'extension) — la progression s'affiche à l'écran."):format(#profs))
    progressUpdate(("recettes — préparation (%d métier(s))…"):format(#profs), 0)
end

-- ------------------------------------------------------------------- panneau
-- La fenêtre est construite À LA DEMANDE (jamais au chargement) : si sa
-- construction échoue, l'addon continue de fonctionner sans fenêtre.
local function buildPanel()
    if ui then return true end
    local okB, errB = pcall(function()
        local okF, frame = pcall(CreateFrame, "Frame", "CohorsFrame", UIParent,
            BackdropTemplateMixin and "BackdropTemplate" or nil)
        if not okF or not frame then
            -- nom déjà pris (vieille copie ?) : fenêtre sans nom, on ne plante jamais
            frame = CreateFrame("Frame", nil, UIParent, BackdropTemplateMixin and "BackdropTemplate" or nil)
        end
        ui = frame
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
        title:SetText("Cohors v" .. ADDON_VER .. " — Compagnon de guilde")

        local sub = ui:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
        sub:SetPoint("TOP", 0, -36)
        sub:SetText("Calendrier : collecte les raids + réponses → chaîne à coller sur le site (page Calendrier). Recettes : « 📚 Recettes » → fichier SavedVariables à envoyer (Préparation de raid).")

        eb = CreateFrame("EditBox", nil, ui)
        eb:SetMultiLine(true)
        eb:SetSize(680, 350)
        eb:SetPoint("TOPLEFT", 20, -60)
        eb:SetFontObject(ChatFontNormal)
        eb:SetAutoFocus(false)
        eb:SetTextInsets(6, 6, 6, 6)
        eb:SetText("")
        eb:SetScript("OnEscapePressed", function() eb:ClearFocus() end)

        statusText = ui:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
        statusText:SetPoint("BOTTOMRIGHT", -256, 23)
        statusText:SetText("prêt (v" .. ADDON_VER .. ")")

        local function mkButton(text, x, w, fn)
            local b = CreateFrame("Button", nil, ui, "UIPanelButtonTemplate")
            b:SetSize(w, 26)
            b:SetPoint("BOTTOMLEFT", x, 16)
            b:SetText(text)
            b:SetScript("OnClick", function()
                dtrace("clic « " .. text .. " »")
                local ok, err = pcall(fn)
                if not ok then
                    local t = tostring(err)
                    Cohors_DB.last_error = "clic " .. text .. " : " .. t
                    msg("ERREUR (clic « " .. text .. " ») — " .. t)
                end
            end)
            return b
        end

        showSummary = function()
            local lines = {}
            if not Cohors_DB.export then
                lines[#lines + 1] = "Aucune collecte pour le moment — clique « Collecter »."
            else
                local nresp = 0
                for _, e in ipairs(results) do nresp = nresp + #(e.invites or {}) end
                lines[#lines + 1] = ("Dernière collecte : %s · %d réponse(s).")
                    :format(Cohors_DB.export_at and dateStr(Cohors_DB.export_at) or "?", nresp)
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

        mkButton("Collecter", 20, 100, function() Cohors_Collect() end)
        mkButton("Réinitialiser", 128, 110, function() Cohors_Reset() end)
        mkButton("Exporter", 246, 100, function()
            if not Cohors_DB.export then
                msg("rien à exporter pour le moment — clique « Collecter ».")
                return
            end
            eb:SetText(Cohors_DB.export)
            eb:HighlightText()
            eb:SetFocus()
            msg("chaîne sélectionnée — fais Ctrl+C puis colle-la sur le site de la guilde (page Calendrier).")
        end)
        mkButton("Diag → fichier", 354, 120, function()
            dumpDiag(true)
        end)
        mkButton("📚 Recettes", 582, 100, function() Cohors_Recipes() end)
        mkButton("Fermer", 482, 90, function() ui:Hide() end)
    end)
    if not okB then
        Cohors_DB.ui_error = tostring(errB)
        msg("interface impossible : " .. tostring(errB) .. " — mode sans fenêtre (/cohors diag → fichier)")
        ui = nil
        return false
    end
    return true
end

function Cohors_Refresh()
    if ui and ui:IsShown() and not collecting and showSummary then
        pcall(showSummary)
    end
end

-- commande unique et toujours disponible
msg("v" .. ADDON_VER .. " — fichier execute jusqu au bout (dossier " .. tostring(ADDON_NAME or "?") .. "). Si aucun autre message Cohors n apparait ensuite, le probleme est cote client.")

SLASH_Cohors1 = "/cohors"
SlashCmdList["Cohors"] = function(arg)
    arg = (arg or ""):lower()
    dtrace("commande : /cohors " .. arg)
    local okAll, errAll = pcall(function()
        if arg == "" then
            buildPanel()
            if ui then ui:Show() end
            msg(("v%s · dossier « %s » (TOC %s) — « Collecter » lance la collecte (ou /cohors collect).")
                :format(ADDON_VER, tostring(ADDON_NAME or "?"), tostring(tocVersion())))
            if Cohors_DB.export and showSummary then pcall(showSummary) end
            if not engine and (time() - (Cohors_DB.export_at or 0)) > 120 then
                Cohors_Collect()
            end
        elseif arg == "collect" then
            buildPanel()
            if ui then ui:Show() end
            Cohors_Collect()
        elseif arg == "export" then
            buildPanel()
            if ui and eb and Cohors_DB.export then
                ui:Show()
                eb:SetText(Cohors_DB.export)
                eb:HighlightText()
                eb:SetFocus()
            elseif Cohors_DB.export then
                msg("pas de fenêtre — utilise /cohors diag puis envoie le fichier.")
            else
                msg("aucune donnée — /cohors collect d'abord.")
            end
        elseif arg == "diag" then
            dumpDiag(true)
        elseif arg == "recettes" then
            Cohors_Recipes()
        elseif arg == "reset" then
            Cohors_Reset()
        else
            msg("commandes : /cohors · /cohors collect · /cohors export · /cohors recettes · /cohors diag · /cohors reset")
        end
    end)
    if not okAll then
        msg("ERREUR (/cohors " .. arg .. ") — " .. tostring(errAll))
    end
end
