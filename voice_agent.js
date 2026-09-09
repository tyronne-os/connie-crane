// ═══════════════════ CRANE VOICE AGENT ═══════════════════
// Persistent floating orb with bi-directional audio, activity detection, HER-style conversation

const VOICE_AGENT = {
  state: 'idle', // idle, listening, processing, speaking, error
  mediaRecorder: null,
  audioContext: null,
  stream: null,
  conversationHistory: [],
  currentAgent: 'hannibal',
  
  // ── UI Configuration ──
  orb: {
    x: window.innerWidth - 100,
    y: window.innerHeight - 100,
    radius: 40,
    dragging: false,
    offsetX: 0,
    offsetY: 0,
  },
  
  // ── Audio Configuration ──
  audio: {
    sampleRate: 16000,
    channels: 1,
    bufferSize: 4096,
  },
  
  // ── Initialize ──
  async init() {
    console.log('[VA] Initializing voice agent...');
    
    // Create canvas for orb
    this.createOrbUI();
    
    // Request microphone permission
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: false,
        }
      });
      this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
      console.log('[VA] Microphone access granted');
      this.setState('idle');
    } catch (err) {
      console.error('[VA] Microphone access denied:', err);
      this.setState('error');
    }
    
    // Load conversation history
    this.loadHistory();
  },
  
  // ── Create Orb UI ──
  createOrbUI() {
    const canvas = document.createElement('canvas');
    canvas.id = 'voice-agent-orb';
    canvas.width = 120;
    canvas.height = 120;
    canvas.style.cssText = `
      position: fixed;
      bottom: 20px;
      right: 20px;
      cursor: grab;
      z-index: 99999;
      border-radius: 50%;
      box-shadow: 0 4px 20px rgba(0,0,0,0.4);
      background: #08080a;
    `;
    
    document.body.appendChild(canvas);
    this.orbCanvas = canvas;
    
    // Mouse/touch events
    canvas.addEventListener('mousedown', (e) => this.startDrag(e));
    canvas.addEventListener('touchstart', (e) => this.startDrag(e));
    document.addEventListener('mousemove', (e) => this.drag(e));
    document.addEventListener('touchmove', (e) => this.drag(e));
    document.addEventListener('mouseup', () => this.endDrag());
    document.addEventListener('touchend', () => this.endDrag());
    
    // Click to toggle listening
    canvas.addEventListener('click', (e) => {
      if (!this.orb.dragging) {
        this.toggleListening();
      }
    });
    
    // Right-click menu
    canvas.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      this.showMenu(e);
    });
    
    // Start render loop
    this.renderOrb();
  },
  
  // ── Render Orb Animation ──
  renderOrb() {
    const canvas = this.orbCanvas;
    const ctx = canvas.getContext('2d');
    const centerX = canvas.width / 2;
    const centerY = canvas.height / 2;
    const radius = 35;
    
    // Clear
    ctx.fillStyle = '#08080a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    
    // State-based rendering
    const time = Date.now() / 1000;
    
    switch (this.state) {
      case 'idle':
        // Gold pulse
        const pulse = 0.3 + 0.2 * Math.sin(time * 2);
        ctx.fillStyle = `rgba(200, 168, 42, ${pulse})`;
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#C8A82A';
        ctx.lineWidth = 2;
        ctx.stroke();
        break;
        
      case 'listening':
        // Bright gold with waveform
        ctx.fillStyle = '#C8A82A';
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
        ctx.fill();
        
        // Waveform
        ctx.strokeStyle = '#08080a';
        ctx.lineWidth = 2;
        for (let i = 0; i < 8; i++) {
          const angle = (i / 8) * Math.PI * 2;
          const wave = 5 + 3 * Math.sin(time * 4 + i);
          const x1 = centerX + Math.cos(angle) * radius;
          const y1 = centerY + Math.sin(angle) * radius;
          const x2 = centerX + Math.cos(angle) * (radius + wave);
          const y2 = centerY + Math.sin(angle) * (radius + wave);
          ctx.beginPath();
          ctx.moveTo(x1, y1);
          ctx.lineTo(x2, y2);
          ctx.stroke();
        }
        break;
        
      case 'processing':
        // Cyan pulse
        ctx.fillStyle = '#06b6d4';
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius * (0.8 + 0.2 * Math.sin(time * 3)), 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#06b6d4';
        ctx.lineWidth = 2;
        ctx.stroke();
        break;
        
      case 'speaking':
        // Jade green with audio bars
        ctx.fillStyle = '#10b981';
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
        ctx.fill();
        
        // Audio bars
        ctx.fillStyle = '#08080a';
        for (let i = 0; i < 5; i++) {
          const barHeight = 8 + 8 * Math.sin(time * 5 + i);
          const x = centerX - 15 + i * 7;
          const y = centerY - barHeight / 2;
          ctx.fillRect(x, y, 5, barHeight);
        }
        break;
        
      case 'error':
        // Red
        ctx.fillStyle = '#ef4444';
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
        ctx.fill();
        // X mark
        ctx.strokeStyle = '#08080a';
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(centerX - 15, centerY - 15);
        ctx.lineTo(centerX + 15, centerY + 15);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(centerX + 15, centerY - 15);
        ctx.lineTo(centerX - 15, centerY + 15);
        ctx.stroke();
        break;
    }
    
    // Label
    ctx.fillStyle = '#f0ead8';
    ctx.font = 'bold 10px monospace';
    ctx.textAlign = 'center';
    ctx.fillText(this.state.toUpperCase(), centerX, canvas.height - 8);
    
    requestAnimationFrame(() => this.renderOrb());
  },
  
  // ── Dragging ──
  startDrag(e) {
    const rect = this.orbCanvas.getBoundingClientRect();
    const x = e.clientX || e.touches[0].clientX;
    const y = e.clientY || e.touches[0].clientY;
    
    this.orb.dragging = true;
    this.orb.offsetX = x - rect.left;
    this.orb.offsetY = y - rect.top;
  },
  
  drag(e) {
    if (!this.orb.dragging) return;
    
    const x = e.clientX || e.touches[0].clientX;
    const y = e.clientY || e.touches[0].clientY;
    
    this.orb.x = x - this.orb.offsetX;
    this.orb.y = y - this.orb.offsetY;
    
    this.orbCanvas.style.right = (window.innerWidth - this.orb.x - 60) + 'px';
    this.orbCanvas.style.bottom = (window.innerHeight - this.orb.y - 60) + 'px';
  },
  
  endDrag() {
    this.orb.dragging = false;
  },
  
  // ── State Management ──
  setState(newState) {
    this.state = newState;
    console.log(`[VA] State: ${newState}`);
  },
  
  // ── Toggle Listening ──
  async toggleListening() {
    if (this.state === 'idle') {
      this.startListening();
    } else if (this.state === 'listening') {
      this.stopListening();
    }
  },
  
  // ── Start Listening ──
  async startListening() {
    if (!this.stream) {
      this.setState('error');
      return;
    }
    
    this.setState('listening');
    const audioChunks = [];
    
    this.mediaRecorder = new MediaRecorder(this.stream);
    this.mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
    
    this.mediaRecorder.onstop = async () => {
      const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
      this.processAudio(audioBlob);
    };
    
    this.mediaRecorder.start();
    
    // Auto-stop after 10 seconds or on silence
    setTimeout(() => {
      if (this.state === 'listening') {
        this.stopListening();
      }
    }, 10000);
  },
  
  // ── Stop Listening ──
  stopListening() {
    if (this.mediaRecorder && this.state === 'listening') {
      this.mediaRecorder.stop();
      this.setState('processing');
    }
  },
  
  // ── Process Audio ──
  async processAudio(audioBlob) {
    console.log('[VA] Processing audio...');
    
    try {
      // Send to STT endpoint
      const formData = new FormData();
      formData.append('audio', audioBlob);
      
      const sttRes = await fetch('/api/voice/stt', {
        method: 'POST',
        body: formData,
      });
      
      const { transcription, confidence } = await sttRes.json();
      
      if (confidence < 0.5) {
        this.setState('idle');
        return;
      }
      
      console.log('[VA] Transcription:', transcription);
      
      // Add to history
      this.conversationHistory.push({
        role: 'user',
        content: transcription,
        timestamp: Date.now(),
      });
      
      // Route to agent
      await this.getAgentResponse(transcription);
      
    } catch (err) {
      console.error('[VA] STT error:', err);
      this.setState('error');
      setTimeout(() => this.setState('idle'), 2000);
    }
  },
  
  // ── Get Agent Response ──
  async getAgentResponse(userMessage) {
    try {
      const res = await fetch('/api/voice/agent/respond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userMessage,
          history: this.conversationHistory,
          agent: this.currentAgent,
        }),
      });
      
      const { response, agent } = await res.json();
      
      this.currentAgent = agent;
      
      // Add to history
      this.conversationHistory.push({
        role: 'assistant',
        content: response,
        agent: agent,
        timestamp: Date.now(),
      });
      
      // Generate TTS and play
      await this.speak(response);
      
    } catch (err) {
      console.error('[VA] Agent error:', err);
      this.setState('error');
      setTimeout(() => this.setState('idle'), 2000);
    }
  },
  
  // ── Text-to-Speech ──
  async speak(text) {
    this.setState('speaking');
    
    try {
      const res = await fetch('/api/voice/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      
      const audioBlob = await res.blob();
      const audioUrl = URL.createObjectURL(audioBlob);
      
      const audio = new Audio(audioUrl);
      audio.onended = () => {
        this.setState('idle');
        this.saveHistory();
      };
      
      audio.play();
      
    } catch (err) {
      console.error('[VA] TTS error:', err);
      this.setState('error');
      setTimeout(() => this.setState('idle'), 2000);
    }
  },
  
  // ── History Management ──
  loadHistory() {
    const saved = localStorage.getItem('voice-agent-history');
    if (saved) {
      this.conversationHistory = JSON.parse(saved);
    }
  },
  
  saveHistory() {
    localStorage.setItem('voice-agent-history', JSON.stringify(
      this.conversationHistory.slice(-50)  // Keep last 50 messages
    ));
  },
  
  // ── Context Menu ──
  showMenu(e) {
    const menu = document.createElement('div');
    menu.style.cssText = `
      position: fixed;
      left: ${e.clientX}px;
      top: ${e.clientY}px;
      background: #121216;
      border: 1px solid #2a2416;
      border-radius: 4px;
      z-index: 100000;
      min-width: 150px;
    `;
    
    menu.innerHTML = `
      <div style="padding: 8px; cursor: pointer; color: #f0ead8; font-size: 12px;">
        <div onclick="VOICE_AGENT.clearHistory()" style="padding: 4px; border-radius: 2px;">Clear History</div>
        <div onclick="VOICE_AGENT.selectAgent()" style="padding: 4px; border-radius: 2px;">Select Agent</div>
        <div onclick="this.parentElement.remove()" style="padding: 4px; border-radius: 2px;">Close</div>
      </div>
    `;
    
    document.body.appendChild(menu);
    setTimeout(() => menu.remove(), 3000);
  },
  
  clearHistory() {
    this.conversationHistory = [];
    localStorage.removeItem('voice-agent-history');
  },
  
  selectAgent() {
    const agent = prompt('Select agent (hannibal, murdock, face, ba, astra):', this.currentAgent);
    if (agent) {
      this.currentAgent = agent;
    }
  },
};

// ═══════════════════ CONNIE: CO-FOUNDER VOICE AGENT ═══════════════════
// Meta engineer, co-founder, handles GM & VELVET
const CONNIE_AGENT = {
  name: "CONNIE",
  company: "Beryl Labs",
  userName: "TJ",
  knowledgeBase: [],
  
  ...VOICE_AGENT,  // Inherit base agent
  
  // Knowledge Base Panel UI
  createKnowledgePanel() {
    const panel = document.createElement('div');
    panel.id = 'connie-kb-panel';
    panel.style.cssText = `
      position: fixed;
      right: 20px;
      bottom: 120px;
      width: 350px;
      height: 400px;
      background: #121216;
      border: 1px solid #2a2416;
      border-radius: 8px;
      z-index: 99998;
      display: flex;
      flex-direction: column;
      box-shadow: 0 4px 20px rgba(0,0,0,0.4);
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.3s;
    `;
    
    panel.innerHTML = `
      <div style="padding: 12px; border-bottom: 1px solid #2a2416;">
        <div style="font-weight: bold; color: #C8A82A; font-size: 13px;">CONNIE KNOWLEDGE BASE</div>
        <div style="font-size: 11px; color: #6b5940; margin-top: 4px;">Upload files for CONNIE's context</div>
      </div>
      
      <div style="flex: 1; overflow-y: auto; padding: 12px;">
        <div id="kb-dropzone" style="
          border: 2px dashed #2a2416;
          border-radius: 4px;
          padding: 20px;
          text-align: center;
          cursor: pointer;
          transition: border 0.2s;
          color: #6b5940;
          font-size: 12px;
        ">
          <div>📄 Drag files here or click</div>
          <div style="font-size: 10px; margin-top: 4px;">Markdown, PDF, TXT</div>
        </div>
        
        <div id="kb-files" style="margin-top: 12px;"></div>
      </div>
      
      <div style="padding: 8px; border-top: 1px solid #2a2416; font-size: 11px; color: #6b5940;">
        Documents: <span id="kb-count">0</span>
      </div>
    `;
    
    document.body.appendChild(panel);
    this.kbPanel = panel;
    
    // Setup drag-drop
    const dropzone = panel.querySelector('#kb-dropzone');
    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.style.borderColor = '#C8A82A';
    });
    dropzone.addEventListener('dragleave', () => {
      dropzone.style.borderColor = '#2a2416';
    });
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.style.borderColor = '#2a2416';
      this.handleFileDrop(e.dataTransfer.files);
    });
    dropzone.addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'file';
      input.multiple = true;
      input.onchange = (e) => this.handleFileDrop(e.target.files);
      input.click();
    });
  },
  
  async handleFileDrop(files) {
    for (const file of files) {
      const text = await file.text();
      const doc = {
        name: file.name,
        type: file.type,
        size: file.size,
        content: text,
        uploaded: new Date().toISOString()
      };
      
      this.knowledgeBase.push(doc);
      
      // Upload to server
      const formData = new FormData();
      formData.append('file', file);
      formData.append('file_name', file.name);
      
      try {
        await fetch('/api/connie/kb/upload', {
          method: 'POST',
          body: formData
        });
      } catch (err) {
        console.error('KB upload error:', err);
      }
    }
    
    this.updateKBDisplay();
  },
  
  updateKBDisplay() {
    const filesDiv = this.kbPanel.querySelector('#kb-files');
    filesDiv.innerHTML = this.knowledgeBase.map((doc, i) => `
      <div style="
        padding: 8px;
        background: rgba(200,168,42,0.1);
        border: 1px solid #2a2416;
        border-radius: 4px;
        margin-bottom: 4px;
        font-size: 11px;
        display: flex;
        justify-content: space-between;
        align-items: center;
      ">
        <div>
          <div style="color: #C8A82A; font-weight: bold;">${doc.name}</div>
          <div style="color: #6b5940; font-size: 10px;">${Math.round(doc.size / 1024)}KB</div>
        </div>
        <button onclick="CONNIE_AGENT.removeKBDoc(${i})" style="
          background: #ef4444;
          color: white;
          border: none;
          padding: 2px 6px;
          border-radius: 2px;
          cursor: pointer;
          font-size: 10px;
        ">✕</button>
      </div>
    `).join('');
    
    this.kbPanel.querySelector('#kb-count').textContent = this.knowledgeBase.length;
  },
  
  removeKBDoc(index) {
    this.knowledgeBase.splice(index, 1);
    this.updateKBDisplay();
  },
  
  toggleKBPanel() {
    this.kbPanel.style.opacity = this.kbPanel.style.opacity === '0' ? '1' : '0';
    this.kbPanel.style.pointerEvents = this.kbPanel.style.opacity === '1' ? 'auto' : 'none';
  }
};

// Initialize CONNIE
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    CONNIE_AGENT.init();
    CONNIE_AGENT.createKnowledgePanel();
  });
} else {
  CONNIE_AGENT.init();
  CONNIE_AGENT.createKnowledgePanel();
}
