--[[
    Rogue Lite - Extracted Features from Hydroxide
    
    Features:
        1. No Textures (always on) - Sets all part materials to SmoothPlastic
        2. No Post-Processing (always on) - Disables Bloom, Blur, DepthOfField, SunRays,
           ColorCorrection. Allows playing at higher graphics for render distance.
        3. Spectate (right-click playerlist) - Right-click a name on the leaderboard to 
           spectate that player. Right-click the same player (or yourself) to stop.
        4. Player Status Dots - Green/red dots on leaderboard names showing if a player
           is spawned in (green) or at the menu (red). Only visible on hover.
        5. ESP (external overlay) - Streams player positions, health, and rogue names to
           an external Python overlay app (esp_overlay.exe) via WebSocket/HTTP. The overlay
           renders dots, names, health bars, and distance on a transparent always-on-top
           window. See ESP_PLAN.md for details.
    
    Executor Requirements:
        - Minimum: Any executor that can run Lua scripts (text-matching fallback)
        - Recommended: getconnections() + debug.getupvalues() for reliable player detection
          (Solara, Wave, Fluxus, and most modern free executors support this)
        - ESP: WebSocket support recommended (fallback: http_request/request for HTTP POST)
    
    Ban Risk: Essentially zero. All features are purely client-side.
              ESP is external (separate process), no in-game UI modifications.
]]

-- ============================================================
-- CONFIGURATION
-- ============================================================

-- If you load this script via a URL (e.g., loadstring(game:HttpGet("..."))()),
-- paste that URL here so the script can automatically reload when you serverhop!
-- If you put this script in your executor's 'autoexec' folder, you can leave this blank.
local LOAD_URL = "https://raw.githubusercontent.com/lincoln1155/rogue-script/refs/heads/main/rogue_lite.lua" 

-- ============================================================
-- INITIALIZATION
-- ============================================================

if not game:IsLoaded() then
    game.Loaded:Wait()
end

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local StarterGui = game:GetService("StarterGui")
local HttpService = game:GetService("HttpService")
local UserInputService = game:GetService("UserInputService")

local plr = Players.LocalPlayer
repeat task.wait() until plr and plr.Backpack

-- Wait for leaderboard to exist in StarterGui (game needs to be fully loaded)
repeat task.wait() until StarterGui:FindFirstChild("LeaderboardGui")

-- ============================================================
-- CONFIGURATION MANAGER
-- ============================================================

local LiteConfig = {
    QOLEnabled = true         -- Controls No Textures & No Post-Processing
}

local configPath = "HYDROXIDE/rogue_lite_config.json"

local function SaveConfig()
    pcall(function()
        if not isfolder("HYDROXIDE") then makefolder("HYDROXIDE") end
        local json = HttpService:JSONEncode(LiteConfig)
        writefile(configPath, json)
    end)
end

local function LoadConfig()
    pcall(function()
        if isfile(configPath) then
            local data = readfile(configPath)
            local parsed = HttpService:JSONDecode(data)
            for k, v in pairs(parsed) do
                if LiteConfig[k] ~= nil then
                    LiteConfig[k] = v
                end
            end
        else
            SaveConfig()
        end
    end)
end

LoadConfig()

-- ============================================================
-- QOL FEATURES (No Textures & No Post-Processing)
-- ============================================================

local original_materials = {}
local original_post_processing = {}
local Lighting = game:GetService("Lighting")

local function isBlacklisted(part)
    local thrown = workspace:FindFirstChild("Thrown")
    if thrown and part:IsDescendantOf(thrown) then
        local ancestor = part.Parent
        while ancestor and ancestor ~= workspace do
            if ancestor:IsA("Model") and ancestor.Name == "EarthPillar" then
                return false
            end
            ancestor = ancestor.Parent
        end
        return true
    end
    return false
end

local function applyNoTexture(part)
    if not LiteConfig.QOLEnabled then return end
    if not part or not part:IsA("BasePart") then return end
    if not part:IsDescendantOf(game) then return end
    if part.Material == Enum.Material.ForceField then return end
    if isBlacklisted(part) then return end

    if not original_materials[part] then
        original_materials[part] = {
            Material = part.Material,
            Reflectance = part.Reflectance,
        }
    end

    part.Material = Enum.Material.SmoothPlastic
    part.Reflectance = 0
end

local function restoreNoTexture()
    for part, data in pairs(original_materials) do
        if part and part.Parent then
            part.Material = data.Material
            part.Reflectance = data.Reflectance
        end
    end
    original_materials = {}
end

local function disablePostEffect(instance)
    if not LiteConfig.QOLEnabled then return end
    if not instance:IsA("PostEffect") then return end
    
    if original_post_processing[instance] == nil then
        original_post_processing[instance] = instance.Enabled
    end
    
    instance.Enabled = false
end

local function restorePostProcessing()
    for instance, enabled in pairs(original_post_processing) do
        if instance and instance.Parent then
            instance.Enabled = enabled
        end
    end
    original_post_processing = {}
end

local function applyQOL()
    if not LiteConfig.QOLEnabled then return end
    
    -- Apply No Textures
    task.spawn(function()
        local descendants = workspace:GetDescendants()
        local batchSize = 500

        for i = 1, #descendants, batchSize do
            for j = i, math.min(i + batchSize - 1, #descendants) do
                applyNoTexture(descendants[j])
            end
            task.wait()
        end
    end)
    
    -- Apply Post Processing
    for _, child in ipairs(Lighting:GetChildren()) do
        disablePostEffect(child)
    end
    for _, child in ipairs(workspace.CurrentCamera:GetChildren()) do
        disablePostEffect(child)
    end
end

local function restoreQOL()
    restoreNoTexture()
    restorePostProcessing()
end

-- Apply to any new parts that get added (new areas loading, spell effects, etc.)
workspace.DescendantAdded:Connect(function(descendant)
    if LiteConfig.QOLEnabled then
        applyNoTexture(descendant)
    end
end)

-- Initial QOL Apply
if LiteConfig.QOLEnabled then
    applyQOL()
    print("[RogueLite] QOL Features enabled.")
else
    print("[RogueLite] QOL Features are off in config.")
end

-- ============================================================
-- SPECTATE (right-click leaderboard names)
-- ============================================================

local spectating = nil          -- The Player we're currently spectating (nil = not spectating)
local spectateConnections = {}  -- Connections to clean up when we stop spectating
local labelPlayerMap = {}       -- label → {player = Player, dot = Frame}
local leaderboardHovered = false

-- Try to find which Player a leaderboard label belongs to
local function getPlayerFromLabel(label)
    -- Method 1: Use getconnections + debug.getupvalues (reliable, requires standard executor)
    -- The game's leaderboard script attaches MouseEnter connections to each label.
    -- These connection functions store the player's username or Player instance as upvalues.
    if typeof(getconnections) == "function" and typeof(debug.getupvalues) == "function" then
        local success, result = pcall(function()
            for _, connection in pairs(getconnections(label.MouseEnter)) do
                if connection.Function then
                    local upvalues = debug.getupvalues(connection.Function)
                    for _, value in pairs(upvalues) do
                        if type(value) == "string" then
                            -- Strip invisible Unicode characters (right-to-left marks, etc.)
                            local username = value:gsub("\226\128\142", "")
                            local player = Players:FindFirstChild(username)
                            if player and player:IsA("Player") then
                                return player
                            end
                        elseif typeof(value) == "Instance" and value:IsA("Player") then
                            return value
                        end
                    end
                end
            end
            return nil
        end)

        if success and result then
            return result
        end
    end

    -- Method 2: Text matching fallback (works on any executor)
    -- Strip invisible characters and try to match against player names
    local text = label.Text
    if not text or text == "" then return nil end

    text = text:gsub("\226\128\142", "")  -- Remove right-to-left mark
    text = text:gsub("%s+", " ")          -- Normalize whitespace
    text = text:match("^%s*(.-)%s*$")     -- Trim

    for _, player in ipairs(Players:GetPlayers()) do
        if player.Name == text or player.DisplayName == text then
            return player
        end
    end

    -- Partial match: label text might contain the name among other info
    for _, player in ipairs(Players:GetPlayers()) do
        if text:find(player.Name, 1, true) or text:find(player.DisplayName, 1, true) then
            return player
        end
    end

    return nil
end

-- Stop spectating and return camera to own character
local function stopSpectating()
    spectating = nil

    -- Clean up connections
    for _, conn in pairs(spectateConnections) do
        pcall(function() conn:Disconnect() end)
    end
    spectateConnections = {}

    -- Return camera to own character
    if plr.Character then
        local humanoid = plr.Character:FindFirstChildOfClass("Humanoid")
        if humanoid then
            workspace.CurrentCamera.CameraSubject = humanoid
        end
    end
end

-- Start spectating a player
local function startSpectating(player)
    stopSpectating() -- Clean up any existing spectate first

    if not player or not player.Character then return end

    local humanoid = player.Character:FindFirstChildOfClass("Humanoid")
    if not humanoid then return end

    spectating = player
    workspace.CurrentCamera.CameraType = Enum.CameraType.Custom
    workspace.CurrentCamera.CameraSubject = humanoid

    -- If the spectated player dies/respawns, follow their new character
    spectateConnections[#spectateConnections + 1] = player.CharacterAdded:Connect(function(newChar)
        task.wait(0.5) -- Wait for Humanoid to be added
        if spectating ~= player then return end -- We stopped spectating in the meantime

        local newHumanoid = newChar:WaitForChild("Humanoid", 5)
        if newHumanoid and spectating == player then
            workspace.CurrentCamera.CameraSubject = newHumanoid
        end
    end)

    -- If the spectated player leaves the game, stop spectating
    spectateConnections[#spectateConnections + 1] = Players.PlayerRemoving:Connect(function(leavingPlayer)
        if leavingPlayer == spectating then
            stopSpectating()
        end
    end)
end

-- Update a single dot's color based on whether the player has a character
local function updateDotColor(label)
    local data = labelPlayerMap[label]
    if not data or not data.dot then return end

    -- Resolve player if not cached yet (or if they left and a new label took their place)
    if not data.player or not data.player.Parent then
        data.player = getPlayerFromLabel(label)
    end

    if not data.player then
        data.dot.BackgroundColor3 = Color3.fromRGB(100, 100, 100) -- Gray = unknown
        return
    end

    if data.player.Character then
        data.dot.BackgroundColor3 = Color3.fromRGB(75, 200, 75)   -- Green = spawned in
    else
        data.dot.BackgroundColor3 = Color3.fromRGB(200, 75, 75)   -- Red = at menu
    end
end

-- Show or hide all status dots
local function setDotsVisible(visible)
    for label, data in pairs(labelPlayerMap) do
        if data.dot and label.Parent then
            data.dot.Visible = visible
        end
    end
end

-- Add an invisible right-click button and a status dot on a leaderboard label
local function setupLabelButton(label)
    if not label:IsA("TextLabel") then return end
    if label:FindFirstChild("_SpectateBtn") then return end

    -- Invisible button for right-click spectate
    local button = Instance.new("TextButton")
    button.Name = "_SpectateBtn"
    button.Text = ""
    button.BackgroundTransparency = 1
    button.Size = UDim2.new(1, 0, 1, 0)
    button.Position = UDim2.new(0, 0, 0, 0)
    button.ZIndex = label.ZIndex + 1
    button.Parent = label

    -- Status dot (small circle, hidden by default)
    local dot = Instance.new("Frame")
    dot.Name = "_StatusDot"
    dot.Size = UDim2.new(0, 6, 0, 6)
    dot.AnchorPoint = Vector2.new(0, 0.5)
    dot.Position = UDim2.new(1, -10, 0.5, 0)
    dot.BackgroundColor3 = Color3.fromRGB(100, 100, 100)
    dot.BorderSizePixel = 0
    dot.Visible = false
    dot.ZIndex = label.ZIndex + 2
    dot.Parent = label

    local corner = Instance.new("UICorner")
    corner.CornerRadius = UDim.new(1, 0)
    corner.Parent = dot

    -- Resolve the player this label belongs to and store it
    local player = getPlayerFromLabel(label)
    labelPlayerMap[label] = { player = player, dot = dot }

    -- If we're currently hovering the leaderboard, show and color the dot immediately
    if leaderboardHovered then
        updateDotColor(label)
        dot.Visible = true
    end

    button.MouseButton2Click:Connect(function()
        local targetPlayer = labelPlayerMap[label] and labelPlayerMap[label].player
        if not targetPlayer then
            targetPlayer = getPlayerFromLabel(label)
            if targetPlayer and labelPlayerMap[label] then
                labelPlayerMap[label].player = targetPlayer
            end
        end
        if not targetPlayer then return end

        -- Toggle: right-click same player again (or yourself) → stop spectating
        if spectating == targetPlayer or targetPlayer == plr then
            stopSpectating()
        else
            startSpectating(targetPlayer)
        end
    end)
end

-- Find the leaderboard and set up buttons on all player labels
local function initLeaderboard()
    local leaderboardGui = plr.PlayerGui:FindFirstChild("LeaderboardGui")
    if not leaderboardGui then
        leaderboardGui = plr.PlayerGui:WaitForChild("LeaderboardGui", 30)
    end
    if not leaderboardGui then
        warn("[RogueLite] LeaderboardGui not found")
        return
    end

    local mainFrame = leaderboardGui:WaitForChild("MainFrame", 10)
    if not mainFrame then
        warn("[RogueLite] MainFrame not found")
        return
    end

    local scrollFrame = mainFrame:WaitForChild("ScrollingFrame", 10)
    if not scrollFrame then
        warn("[RogueLite] ScrollingFrame not found")
        return
    end

    -- Process all existing labels
    for _, label in ipairs(scrollFrame:GetChildren()) do
        setupLabelButton(label)
    end

    -- Process labels that get added later (players joining, leaderboard refresh)
    scrollFrame.ChildAdded:Connect(function(label)
        task.wait(0.2) -- Small delay to let the label initialize
        setupLabelButton(label)
    end)

    -- Clean up tracking when labels are removed (player left, leaderboard refresh)
    scrollFrame.ChildRemoved:Connect(function(label)
        labelPlayerMap[label] = nil
    end)

    -- Hover detection: show dots only when mouse is over the leaderboard
    mainFrame.MouseEnter:Connect(function()
        leaderboardHovered = true
        for label, _ in pairs(labelPlayerMap) do
            if label.Parent then
                updateDotColor(label)
            end
        end
        setDotsVisible(true)
    end)

    mainFrame.MouseLeave:Connect(function()
        leaderboardHovered = false
        setDotsVisible(false)
    end)

    print("[RogueLite] Spectate + status dots initialized")
end

-- Initialize the leaderboard
task.spawn(initLeaderboard)

-- Re-initialize if the LeaderboardGui is recreated (happens on certain game events)
plr.PlayerGui.ChildAdded:Connect(function(child)
    if child.Name == "LeaderboardGui" then
        task.wait(1) -- Wait for it to fully initialize
        initLeaderboard()
    end
end)

-- When OUR character respawns while spectating, the game resets CameraSubject.
-- Override that so we keep spectating the target player.
plr.CharacterAdded:Connect(function()
    if spectating and spectating.Character then
        task.wait(0.5)
        if spectating then -- Still spectating after the wait
            local humanoid = spectating.Character:FindFirstChildOfClass("Humanoid")
            if humanoid then
                workspace.CurrentCamera.CameraType = Enum.CameraType.Custom
                workspace.CurrentCamera.CameraSubject = humanoid
            end
        end
    end
end)

-- ============================================================
-- SERVERHOP PERSISTENCE
-- ============================================================

local queue_func = queue_on_teleport or queueteleport
if queue_func and LOAD_URL and LOAD_URL ~= "" then
    plr.OnTeleport:Connect(function(state)
        -- Only queue when actually leaving
        if state == Enum.TeleportState.Started or state == Enum.TeleportState.InProgress then
            local loader_script = string.format([[
                if not game:IsLoaded() then game.Loaded:Wait() end
                task.wait(1)
                pcall(function()
                    loadstring(game:HttpGet("%s"))()
                end)
            ]], LOAD_URL)
            
            pcall(function()
                queue_func(loader_script)
            end)
        end
    end)
    print("[RogueLite] Serverhop persistence enabled (URL configured)")
elseif queue_func then
    print("[RogueLite] Serverhop persistence skipped (No LOAD_URL configured. Use autoexec folder instead!)")
else
    print("[RogueLite] Serverhop persistence skipped (Executor does not support queue_on_teleport)")
end


-- ============================================================
-- ESP DATA SENDER (external overlay)
-- ============================================================

local HttpService = game:GetService("HttpService")

local ESP_PORT = 27015
local ESP_WS_PORT = ESP_PORT + 1
local ESP_UPDATE_INTERVAL = 0.05 -- 50ms (~20 updates/sec)
local LOCAL_APP_DATA = type(os.getenv) == "function" and os.getenv("LOCALAPPDATA") or ""
local ESP_EXE_PATH = LOCAL_APP_DATA ~= "" and (LOCAL_APP_DATA .. "\\RogueLiteESP\\esp_overlay.exe") or ""

-- Resolve rogue in-game name for a player by checking leaderboard labels
-- labelPlayerMap is defined in the SPECTATE section above
local function getRogueName(player)
    for label, data in pairs(labelPlayerMap) do
        if data.player == player and label:IsA("TextLabel") then
            local text = label.Text
            if text and text ~= "" then
                -- Strip invisible Unicode characters (right-to-left marks, etc.)
                text = text:gsub("\226\128\142", "")
                text = text:match("^%s*(.-)%s*$") -- Trim whitespace
                return text
            end
        end
    end
    -- Fallback: use Roblox display name if leaderboard hasn't mapped yet
    return player.DisplayName
end

-- Try to auto-launch the overlay .exe if it's not already running
local function tryLaunchOverlay()
    if ESP_EXE_PATH == "" then
        print("[RogueLite] ESP overlay auto-launch skipped (os.getenv not available in this executor).")
        print("[RogueLite] Please manually run esp_overlay.exe before or after injecting this script.")
        return false
    end

    -- Check if the exe exists
    local file = io.open(ESP_EXE_PATH, "r")
    if not file then
        print("[RogueLite] ESP overlay not found at: " .. ESP_EXE_PATH)
        print("[RogueLite] Start it manually or place esp_overlay.exe in %LOCALAPPDATA%\\RogueLiteESP\\")
        return false
    end
    file:close()

    -- Try to launch it (various executor methods)
    local launched = false
    
    -- Method 1: os.execute (backgrounded)
    if not launched then
        local ok = pcall(function()
            os.execute('start "" "' .. ESP_EXE_PATH .. '"')
        end)
        if ok then launched = true end
    end

    -- Method 2: executor-specific 'run' function
    if not launched and typeof(run) == "function" then
        local ok = pcall(function()
            run(ESP_EXE_PATH)
        end)
        if ok then launched = true end
    end

    if launched then
        print("[RogueLite] ESP overlay launched from: " .. ESP_EXE_PATH)
        task.wait(2) -- Give it time to start
    else
        print("[RogueLite] Could not auto-launch ESP overlay. Please start it manually.")
    end
    
    return launched
end

-- Build the data payload
local function buildESPPayload()
    local camera = workspace.CurrentCamera
    if not camera then return nil end

    local cf = camera.CFrame
    local x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22 = cf:GetComponents()
    local fov = camera.FieldOfView
    local vpSize = camera.ViewportSize

    local myChar = plr.Character
    local myRoot = myChar and myChar:FindFirstChild("HumanoidRootPart")
    local myPos = myRoot and myRoot.Position

    local playerList = {}
    for _, otherPlayer in ipairs(Players:GetPlayers()) do
        if otherPlayer ~= plr then
            local char = otherPlayer.Character
            if char then
                local root = char:FindFirstChild("HumanoidRootPart")
                local humanoid = char:FindFirstChildOfClass("Humanoid")
                if root and humanoid then
                    local pos = root.Position
                    local dist = myPos and (pos - myPos).Magnitude or 0
                    local hp = humanoid.MaxHealth > 0 and (humanoid.Health / humanoid.MaxHealth) or 0

                    table.insert(playerList, {
                        name = getRogueName(otherPlayer),
                        pos = {pos.X, pos.Y, pos.Z},
                        hp = hp,
                        dist = dist,
                    })
                end
            end
        end
    end

    return {
        camera = {
            cf = {x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22},
            fov = fov,
            vp = {vpSize.X, vpSize.Y},
        },
        players = playerList,
    }
end

-- Connection state
local espConnection = nil -- WebSocket connection object
local espUseHTTP = false  -- Fallback flag

-- Try to establish WebSocket connection
local function connectWebSocket()
    if not WebSocket then
        print("[RogueLite] ESP: WebSocket not available, using HTTP fallback")
        espUseHTTP = true
        return
    end

    local ok, ws = pcall(function()
        return WebSocket.connect("ws://127.0.0.1:" .. tostring(ESP_WS_PORT))
    end)

    if ok and ws then
        espConnection = ws
        print("[RogueLite] ESP: WebSocket connected to overlay")

        -- Handle disconnect
        task.spawn(function()
            local closeOk, _ = pcall(function()
                ws.OnClose:Wait()
            end)
            espConnection = nil
            print("[RogueLite] ESP: WebSocket disconnected, will retry...")
        end)
    else
        print("[RogueLite] ESP: WebSocket failed, using HTTP fallback")
        espUseHTTP = true
    end
end

-- Send data via HTTP POST (fallback)
local function sendHTTP(payload)
    local json = HttpService:JSONEncode(payload)
    local req = http_request or request
    if not req then return false end

    local ok, response = pcall(req, {
        Url = "http://127.0.0.1:" .. tostring(ESP_PORT) .. "/update",
        Method = "POST",
        Headers = {
            ["Content-Type"] = "application/json",
        },
        Body = json,
    })

    return ok and response and response.Success
end

-- Send data via WebSocket (primary)
local function sendWebSocket(payload)
    if not espConnection then return false end
    local json = HttpService:JSONEncode(payload)
    local ok = pcall(function()
        espConnection:Send(json)
    end)
    return ok
end

-- Main ESP data loop
task.spawn(function()
    -- Try to auto-launch overlay
    tryLaunchOverlay()

    -- Try WebSocket first
    connectWebSocket()

    -- Data send loop
    while true do
        task.wait(ESP_UPDATE_INTERVAL)

        local payload = buildESPPayload()
        if not payload then continue end

        if not espUseHTTP and espConnection then
            -- Primary: WebSocket
            local ok = sendWebSocket(payload)
            if not ok then
                -- WebSocket broke, try reconnecting
                espConnection = nil
                task.spawn(connectWebSocket)
            end
        elseif espUseHTTP then
            -- Fallback: HTTP POST
            sendHTTP(payload)
        else
            -- No connection, try reconnecting WebSocket periodically
            task.spawn(connectWebSocket)
            task.wait(5) -- Don't spam reconnect attempts
        end
    end
end)

print("[RogueLite] ESP Data Sender initialized (port " .. tostring(ESP_PORT) .. ")")


-- ============================================================
-- HOTKEYS
-- ============================================================

UserInputService.InputBegan:Connect(function(input, gameProcessedEvent)
    if gameProcessedEvent then return end

    if UserInputService:IsKeyDown(Enum.KeyCode.LeftControl) or UserInputService:IsKeyDown(Enum.KeyCode.RightControl) then
        -- Toggle QOL (CTRL + Y)
        if input.KeyCode == Enum.KeyCode.Y then
            LiteConfig.QOLEnabled = not LiteConfig.QOLEnabled
            SaveConfig()
            
            if LiteConfig.QOLEnabled then
                applyQOL()
                -- Notification could be added here if needed
                print("[RogueLite] QOL Enabled")
            else
                restoreQOL()
                print("[RogueLite] QOL Disabled")
            end
        end


    end
end)

-- ============================================================
-- DONE
-- ============================================================

print("[RogueLite] Loaded successfully - No Textures + No Post-Processing + Spectate + ESP")
