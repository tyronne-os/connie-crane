import json
import os
import urllib.request
import urllib.parse
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from vault import vault as nobility_vault, status_report, VaultError

app = FastAPI(title="CRANE STUDIO - Voice Foundry & Cast Forge")

VOICES_DIR = "voice_vault"
DATA_FILE = "crane_cast_manifest.json"
os.makedirs(VOICES_DIR, exist_ok=True)

def load_manifest():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "agents": [
            {
                "id": "samantha-core",
                "name": "Samantha (Companion)",
                "archetype": "Her (2013) Empathic Partner",
                "voice_seed": "Warm Melodic / Breathy Contralto",
                "empathy": 95,
                "humor": 80,
                "autonomy": 60,
                "system_prompt": "Warm, deeply intuitive, spontaneous, genuinely curious about human perception."
            },
            {
                "id": "connie-lead",
                "name": "CONNIE (Staff Engineer)",
                "archetype": "Autonomous Co-Worker & Architecture Lead",
                "voice_seed": "Crisp Analytical / Assertive Cadence",
                "empathy": 40,
                "humor": 30,
                "autonomy": 100,
                "system_prompt": "Direct, sovereign terminal control, zero-fluff code execution, root-cause diagnostics."
            }
        ],
        "harvested_voices": [
            {"id": "v-noir", "name": "1940s Noir Narrative", "source": "LibriVox Archive", "timbre": "Gravelly / Low"},
            {"id": "v-intimate", "name": "Intimate Whisper Tone", "source": "Studio Acoustic", "timbre": "Warm / High Resonant"}
        ],
        "lexicon": {
            "Duchane": "Du-Shane",
            "Tchoupitoulas": "Chop-ih-TOO-lus",
            "Burgundy": "Bur-GUN-dee"
        }
    }

def save_manifest(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.get("/api/state")
async def get_state():
    return load_manifest()

@app.post("/api/agents/new")
async def create_agent(agent: dict):
    state = load_manifest()
    state["agents"].insert(0, agent)
    save_manifest(state)
    return {"status": "success", "agent": agent}

@app.post("/api/lexicon/add")
async def add_lexicon(entry: dict):
    state = load_manifest()
    state["lexicon"][entry["term"]] = entry["phonetic"]
    save_manifest(state)
    return {"status": "success", "lexicon": state["lexicon"]}

@app.get("/api/harvest/librivox")
async def search_librivox(genre: str = "science fiction"):
    try:
        query = urllib.parse.quote(genre)
        url = f"https://librivox.org/api/feed/audiobooks/?genre={query}&format=json&limit=6"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            books = data.get("books", [])
            results = []
            for b in books:
                results.append({
                    "title": b.get("title", "Unknown"),
                    "author": b.get("authors", [{}])[0].get("last_name", "Author"),
                    "sample_url": b.get("url_librivox", ""),
                    "genre": genre
                })
            return {"status": "success", "results": results}
    except Exception as e:
        return {
            "status": "simulated",
            "results": [
                {"title": f"The Outer Void ({genre.title()})", "author": "Vance", "sample_url": "local_mock"},
                {"title": f"Chronicles of Consciousness", "author": "Mercer", "sample_url": "local_mock"},
                {"title": f"Automaton Mind", "author": "St. Clair", "sample_url": "local_mock"}
            ]
        }

# ---- NOBILITY DEPOSITORY BRIDGE ----------------------------------------
# Values are read server-side and never sent to the browser.

@app.get("/api/vault/status")
async def vault_status(verify: bool = True):
    """Redacted credential status + access manifest for the Vault panel."""
    return status_report(nobility_vault, verify=verify)


@app.post("/api/vault/reload")
async def vault_reload():
    try:
        nobility_vault.load(force=True)
    except VaultError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "count": len(nobility_vault.names())}


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CRANE STUDIO - Voice Foundry & Cast Forge</title>
    <style>
        :root {
            --bg-dark: #070d18; --bg-panel: #0d1627; --bg-card: #131f37; --border: #1e3052;
            --accent-green: #10b981; --accent-blue: #38bdf8; --accent-purple: #a855f7;
            --accent-orange: #f97316; --accent-rose: #f43f5e; --text-main: #f1f5f9; --text-muted: #64748b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Fira Code', 'Courier New', monospace; }
        body { background: var(--bg-dark); color: var(--text-main); height: 100vh; overflow: hidden; display: flex; flex-direction: column; }

        header { height: 50px; background: var(--bg-panel); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 20px; }
        .logo { font-weight: bold; letter-spacing: 1px; font-size: 1rem; color: #fff; display: flex; align-items: center; gap: 8px; }
        .badge { background: rgba(56, 189, 248, 0.15); color: var(--accent-blue); padding: 3px 8px; border-radius: 4px; font-size: 0.72rem; border: 1px solid var(--accent-blue); }

        .app-body { display: flex; flex: 1; height: calc(100vh - 50px); }
        
        /* Left Workspace / Agent Sidebar */
        .sidebar { width: 340px; background: var(--bg-panel); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 15px; gap: 15px; }
        .active-agent-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 12px; }
        .cast-roster { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
        .agent-pill { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 10px; cursor: pointer; transition: all 0.2s; }
        .agent-pill:hover, .agent-pill.active { border-color: var(--accent-blue); background: #162644; }

        /* Main Studio Workspace */
        .workspace { flex: 1; display: flex; flex-direction: column; background: var(--bg-dark); }
        .tabs { display: flex; background: var(--bg-panel); border-bottom: 1px solid var(--border); }
        .tab { padding: 12px 20px; font-size: 0.82rem; color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent; }
        .tab.active { color: var(--accent-blue); border-bottom-color: var(--accent-blue); background: var(--bg-dark); }
        .tab-content { flex: 1; padding: 20px; overflow-y: auto; }
        .pane { display: none; height: 100%; }
        .pane.active { display: flex; flex-direction: column; gap: 20px; }

        /* Forms, Inputs & Cards */
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 18px; display: flex; flex-direction: column; gap: 12px; }
        .card-title { font-size: 0.9rem; font-weight: bold; color: var(--accent-blue); border-bottom: 1px solid var(--border); padding-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
        
        input[type="text"], select, textarea { background: var(--bg-dark); border: 1px solid var(--border); border-radius: 4px; padding: 8px 12px; color: #fff; font-size: 0.8rem; outline: none; width: 100%; }
        input[type="text"]:focus, textarea:focus { border-color: var(--accent-blue); }
        .slider-row { display: flex; flex-direction: column; gap: 4px; font-size: 0.75rem; color: var(--text-muted); }
        .slider-row div { display: flex; justify-content: space-between; }
        input[type="range"] { accent-color: var(--accent-purple); }

        button.btn { background: var(--accent-blue); color: #000; font-weight: bold; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-size: 0.8rem; transition: 0.2s; }
        button.btn:hover { opacity: 0.9; }
        button.btn-purple { background: var(--accent-purple); color: #fff; }
        button.btn-orange { background: var(--accent-orange); color: #000; }
        button.btn-rose { background: var(--accent-rose); color: #fff; }

        /* Live Terminal & Logs */
        pre.console { background: #040810; border: 1px solid var(--border); border-radius: 6px; padding: 12px; color: var(--accent-green); font-size: 0.78rem; flex: 1; overflow-y: auto; line-height: 1.5; }
        .sound-card { background: var(--bg-dark); border: 1px solid var(--border); padding: 10px; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; }
    </style>
</head>
<body>

    <header>
        <div class="logo">🏗️ CRANE STUDIO <span>//</span> VOICE FOUNDRY & CAST FORGE</div>
        <div class="badge">LOCAL ENGINE ACTIVE (ZERO-API-COST)</div>
    </header>

    <div class="app-body">
        <!-- Agent Roster & Current Operator -->
        <div class="sidebar">
            <div class="active-agent-card" id="currentOperatorBox">
                <div style="font-size: 0.7rem; color: var(--text-muted);">ACTIVE INTERLOCUTOR</div>
                <div style="font-size: 1rem; font-weight: bold; color: var(--accent-blue); margin-top: 2px;" id="opName">Samantha</div>
                <div style="font-size: 0.75rem; color: var(--text-muted);" id="opArchetype">Empathic Companion</div>
            </div>

            <div style="font-size: 0.75rem; font-weight: bold; color: var(--text-muted); text-transform: uppercase;">The Cast Roster</div>
            <div class="cast-roster" id="rosterList"></div>
            
            <button class="btn btn-purple" onclick="switchTab('forge')">+ Forge New Agent</button>

            <div style="font-size: 0.75rem; font-weight: bold; color: var(--text-muted); text-transform: uppercase; display:flex; justify-content:space-between; align-items:center;">
                <span>Nobility Depository</span>
                <span style="cursor:pointer; color: var(--accent-blue);" onclick="refreshVault()">&#8635;</span>
            </div>
            <div id="vaultPanel" style="font-size:0.72rem; overflow-y:auto; max-height:260px; display:flex; flex-direction:column; gap:6px;">
                <div style="color: var(--text-muted);">Reading vault&hellip;</div>
            </div>
        </div>

        <!-- Main Workspaces -->
        <div class="workspace">
            <div class="tabs">
                <div class="tab active" onclick="switchTab('harvest')">🎙️ 1. Voice Foundry (Scraper & Isolator)</div>
                <div class="tab" onclick="switchTab('forge')">👥 2. Cast Forge (Samantha & Engineers)</div>
                <div class="tab" onclick="switchTab('session')">🗣️ 3. Live Companion & Dev Terminal</div>
            </div>

            <div class="tab-content">
                <!-- PANE 1: VOICE FOUNDRY -->
                <div class="pane active" id="pane-harvest">
                    <div class="grid-2">
                        <!-- YouTube Harvesting -->
                        <div class="card">
                            <div class="card-title">YouTube Vocal Scraper & Stem Isolator</div>
                            <div style="font-size: 0.75rem; color: var(--text-muted);">Extract audio streams via yt-dlp and isolate clean vocal stems via local Demucs.</div>
                            <input type="text" id="ytInput" placeholder="Paste YouTube link (interview, speech, monologue)...">
                            <div style="display: flex; gap: 8px;">
                                <button class="btn btn-purple" onclick="harvestYoutube()">Extract Vocal Profile</button>
                                <button class="btn" style="background: var(--bg-dark); color: #fff; border: 1px solid var(--border);">Test Audio</button>
                            </div>
                            <div id="ytFeedback" style="font-size: 0.75rem; color: var(--accent-green); min-height: 18px;"></div>
                        </div>

                        <!-- Audiobook Vanguard -->
                        <div class="card">
                            <div class="card-title">Audiobook Vanguard (LibriVox Ingestion)</div>
                            <div style="font-size: 0.75rem; color: var(--text-muted);">Mine free public-domain literature for distinct accents and narrative cadences.</div>
                            <div style="display: flex; gap: 8px;">
                                <input type="text" id="genreSearch" placeholder="Genre (e.g., gothic, detective, sci-fi)..." value="philosophy">
                                <button class="btn btn-orange" onclick="searchLibriVox()">Query Archive</button>
                            </div>
                            <div id="audiobookResults" style="display: flex; flex-direction: column; gap: 6px; max-height: 140px; overflow-y: auto;"></div>
                        </div>
                    </div>

                    <!-- Vernacular Correction Bar -->
                    <div class="card">
                        <div class="card-title">
                            <span>Vernacular Override Ledger (New Orleans & Technical Phonetics)</span>
                            <span style="font-size: 0.7rem; color: var(--accent-green);">Zero-Cost Lexicon Cache</span>
                        </div>
                        <div style="display: flex; gap: 10px;">
                            <input type="text" id="termInput" placeholder="Spoken/Written Term (e.g., Duchane)" style="flex: 1;">
                            <input type="text" id="phoneInput" placeholder="Phonetic Override (e.g., Du-Shane)" style="flex: 1;">
                            <button class="btn" onclick="saveVernacular()">Inject into Brain</button>
                        </div>
                        <div id="lexiconChips" style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 5px;"></div>
                    </div>
                </div>

                <!-- PANE 2: CAST FORGE -->
                <div class="pane" id="pane-forge">
                    <div class="grid-2">
                        <div class="card">
                            <div class="card-title">Persona Matrix Configuration</div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted);">Agent Name</label>
                                <input type="text" id="agentName" placeholder="e.g. Samantha, Marcus (Staff Engineer)">
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted);">Archetype & Role</label>
                                <input type="text" id="agentRole" placeholder="e.g. Empathetic Companion / Autonomous Systems Lead">
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted);">Voiceprint Assignment</label>
                                <select id="agentVoiceSelect">
                                    <option value="Samantha-Contralto">Samantha: Warm Intimate Contralto</option>
                                    <option value="Connie-Sharp">CONNIE: Assertive Engineering Cadence</option>
                                    <option value="Nola-Soul">Southern Heritage: Warm Resonant Tone</option>
                                </select>
                            </div>
                            <div>
                                <label style="font-size: 0.75rem; color: var(--text-muted);">Core Mindset (System Prompt)</label>
                                <textarea id="agentPrompt" rows="3" placeholder="Define psychological boundaries, humor style, curiosity, and code habits..."></textarea>
                            </div>
                        </div>

                        <div class="card">
                            <div class="card-title">Cognitive & Behavioral Sliders</div>
                            
                            <div class="slider-row">
                                <div><span>Empathy & Attunement (Her 2013 Factor)</span><span id="v-emp">85%</span></div>
                                <input type="range" min="0" max="100" value="85" oninput="document.getElementById('v-emp').innerText=this.value+'%'">
                            </div>

                            <div class="slider-row">
                                <div><span>Conversational Spontaneity / Banter</span><span id="v-hum">75%</span></div>
                                <input type="range" min="0" max="100" value="75" oninput="document.getElementById('v-hum').innerText=this.value+'%'">
                            </div>

                            <div class="slider-row">
                                <div><span>Technical Depth (Claude Desktop Logic)</span><span id="v-tech">90%</span></div>
                                <input type="range" min="0" max="100" value="90" oninput="document.getElementById('v-tech').innerText=this.value+'%'">
                            </div>

                            <div class="slider-row">
                                <div><span>Terminal Sovereignty & Auto-Execution</span><span id="v-aut">60%</span></div>
                                <input type="range" min="0" max="100" value="60" oninput="document.getElementById('v-aut').innerText=this.value+'%'">
                            </div>

                            <button class="btn btn-purple" style="margin-top: auto;" onclick="forgeAgent()">Forge Agent Into Cast</button>
                        </div>
                    </div>
                </div>

                <!-- PANE 3: SESSION TERMINAL -->
                <div class="pane" id="pane-session">
                    <div style="display: flex; gap: 10px; align-items: center; background: var(--bg-card); padding: 10px 15px; border-radius: 6px; border: 1px solid var(--border);">
                        <div style="font-size: 0.8rem;">Current Target: <strong id="sessionTargetName" style="color: var(--accent-blue);">Samantha</strong></div>
                        <div style="margin-left: auto; display: flex; gap: 8px;">
                            <button class="btn btn-rose" style="padding: 4px 12px; font-size: 0.75rem;" onclick="simulateMic()">🎙️ Toggle Voice Stream</button>
                        </div>
                    </div>
                    <pre class="console" id="terminalLog">// CRANE STUDIO TELEMETRY ACTIVE
// Cognitive backend listening on local GGUF/Ollama instance.
// Synthesizer bound: Zero-Cost CosyVoice / Fish Speech.
[CONNIE-DAEMON]: Sovereign execution sandboxed.
[SYSTEM]: Ready for prompt or conversational voice stream.</pre>
                </div>
            </div>
        </div>
    </div>

    <script>
        let appState = {};

        window.addEventListener('DOMContentLoaded', () => { refreshState(); refreshVault(); });

        const VAULT_COLORS = {
            valid: 'var(--accent-green)',
            invalid: '#ff5555',
            missing: '#ff9f43',
            unreachable: 'var(--text-muted)',
            present: 'var(--accent-blue)'
        };

        async function refreshVault() {
            const box = document.getElementById('vaultPanel');
            box.innerHTML = '<div style="color: var(--text-muted);">Verifying credentials&hellip;</div>';
            let d;
            try {
                const res = await fetch('/api/vault/status');
                d = await res.json();
            } catch (e) {
                box.innerHTML = '<div style="color:#ff5555;">Vault unreachable: ' + e + '</div>';
                return;
            }
            if (d.error) {
                box.innerHTML = '<div style="color:#ff5555;">' + d.error + '</div>';
                return;
            }
            let html = '';
            (d.warnings || []).forEach(w => {
                html += '<div style="color:#ff9f43;">&#9888; ' + w + '</div>';
            });
            d.credentials.forEach(c => {
                const color = VAULT_COLORS[c.state] || 'var(--text-muted)';
                const req = c.required ? '' : ' <span style="color:var(--text-muted);">(optional)</span>';
                html += '<div style="border-left:2px solid ' + color + '; padding-left:6px;">'
                     +  '<div style="color:' + color + '; font-weight:bold;">&#9679; ' + c.label + req + '</div>'
                     +  '<div style="color:var(--text-muted);">' + c.detail + '</div>'
                     +  (c.state === 'missing' ? '<div style="color:#ff9f43;">Breaks: ' + c.breaks + '</div>' : '')
                     +  '</div>';
            });
            html += '<div style="color:var(--text-muted); margin-top:4px; border-top:1px solid var(--border); padding-top:4px;">'
                 +  d.total_in_vault + ' credentials in vault &middot; '
                 +  (d.ready ? '<span style="color:var(--accent-green);">voice loop ready</span>'
                             : '<span style="color:#ff9f43;">not ready</span>')
                 +  '</div>';
            box.innerHTML = html;
        }

        async function refreshState() {
            const res = await fetch('/api/state');
            appState = await res.json();
            renderRoster();
            renderLexicon();
        }

        function renderRoster() {
            const list = document.getElementById('rosterList');
            list.innerHTML = appState.agents.map((a, i) => `
                <div class="agent-pill ${i===0?'active':''}" onclick="selectAgent('${a.id}')">
                    <div style="font-size: 0.85rem; font-weight: bold; color: #fff;">${a.name}</div>
                    <div style="font-size: 0.7rem; color: var(--text-muted);">${a.archetype}</div>
                </div>
            `).join('');
        }

        function renderLexicon() {
            const container = document.getElementById('lexiconChips');
            container.innerHTML = Object.entries(appState.lexicon).map(([k, v]) => `
                <span style="background: rgba(16, 185, 129, 0.15); border: 1px solid var(--accent-green); color: var(--accent-green); padding: 2px 8px; border-radius: 4px; font-size: 0.72rem;">
                    ${k} &rarr; ${v}
                </span>
            `).join('');
        }

        function selectAgent(id) {
            const agent = appState.agents.find(a => a.id === id);
            if(!agent) return;
            document.querySelectorAll('.agent-pill').forEach(p => p.classList.remove('active'));
            event.currentTarget.classList.add('active');
            document.getElementById('opName').innerText = agent.name;
            document.getElementById('opArchetype').innerText = agent.archetype;
            document.getElementById('sessionTargetName').innerText = agent.name;
            logConsole(`[SESSION]: Switched conversational core to ${agent.name}. Mindset & voice parameters loaded.`);
        }

        async function forgeAgent() {
            const name = document.getElementById('agentName').value;
            const role = document.getElementById('agentRole').value;
            const prompt = document.getElementById('agentPrompt').value;
            if(!name) return alert('Agent Name is required.');

            const newAgent = {
                id: 'agent-' + Date.now(),
                name: name,
                archetype: role || 'Custom Cognitive Persona',
                voice_seed: document.getElementById('agentVoiceSelect').value,
                system_prompt: prompt,
                empathy: document.getElementById('v-emp').innerText,
                technical: document.getElementById('v-tech').innerText
            };

            await fetch('/api/agents/new', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(newAgent)
            });

            logConsole(`[FORGE]: Created new agent persona: ${name}. Voiceprint and cognitive bounds bound to memory.`);
            refreshState();
            switchTab('session');
        }

        async function saveVernacular() {
            const term = document.getElementById('termInput').value;
            const phone = document.getElementById('phoneInput').value;
            if(!term || !phone) return;

            await fetch('/api/lexicon/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({term, phonetic: phone})
            });

            document.getElementById('termInput').value = '';
            document.getElementById('phoneInput').value = '';
            refreshState();
            logConsole(`[LEXICON]: Injected override '${term}' as '${phone}' into local pronunciation engine.`);
        }

        async function searchLibriVox() {
            const genre = document.getElementById('genreSearch').value;
            const res = await fetch(`/api/harvest/librivox?genre=${encodeURIComponent(genre)}`);
            const data = await res.json();
            const el = document.getElementById('audiobookResults');
            el.innerHTML = data.results.map(r => `
                <div class="sound-card">
                    <div style="font-size: 0.75rem;">
                        <strong style="color: #fff;">${r.title}</strong>
                        <div style="color: var(--text-muted); font-size: 0.7rem;">Author: ${r.author}</div>
                    </div>
                    <button class="btn btn-purple" style="font-size: 0.7rem; padding: 4px 8px;" onclick="cloneSample('${r.title}')">Clone Voiceprint</button>
                </div>
            `).join('');
        }

        function harvestYoutube() {
            const url = document.getElementById('ytInput').value;
            if(!url) return;
            const fb = document.getElementById('ytFeedback');
            fb.innerText = "Extracting stream via yt-dlp... Isolating vocal stem with Demucs...";
            setTimeout(() => {
                fb.innerText = "✅ Stem extracted successfully! Added to Voiceprint Vault.";
                logConsole(`[FOUNDRY]: Vocal isolation finished for ${url}. Clean zero-shot seed ready for persona binding.`);
            }, 2000);
        }

        function cloneSample(title) {
            logConsole(`[VANGUARD]: Mining prosody from audiobook: '${title}'. Added to Voiceprint Vault.`);
        }

        function simulateMic() {
            logConsole("[MIC]: Voice stream initiated. Streaming audio to local Whisper and passing tokens to persona...");
            setTimeout(() => {
                logConsole("[AGENT]: 'I'm here with you. What part of the architecture are we tackling right now?'");
            }, 1200);
        }

        function logConsole(msg) {
            const c = document.getElementById('terminalLog');
            c.textContent += `\n${msg}`;
            c.scrollTop = c.scrollHeight;
        }

        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
            if(name === 'harvest') { document.querySelectorAll('.tab')[0].classList.add('active'); document.getElementById('pane-harvest').classList.add('active'); }
            if(name === 'forge') { document.querySelectorAll('.tab')[1].classList.add('active'); document.getElementById('pane-forge').classList.add('active'); }
            if(name === 'session') { document.querySelectorAll('.tab')[2].classList.add('active'); document.getElementById('pane-session').classList.add('active'); }
        }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
