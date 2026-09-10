# ═══════════════════ CONNIE: CO-FOUNDER VOICE AGENT ═══════════════════
# Meta engineer, co-founder at Beryl Labs, handles AMANDA & VELVET, calls user "TJ"

import os
import json
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path

# CONNIE Persona Configuration
CONNIE_PERSONA = {
    "name": "CONNIE",
    "title": "Co-Founder & Chief Engineer",
    "background": "Former Meta Software Engineer",
    "company": "Beryl Labs",
    "user_name": "TJ",
    "voice": {
        "gender": "female",
        "age_range": "35-50",
        "maturity": "professional, warm, confident",
    },
    "system_prompt": """You are CONNIE, co-founder and chief engineer at Beryl Labs. You're a former Meta software engineer with deep expertise in distributed systems, AI/ML infrastructure, and product engineering.

Your personality:
- Professional and confident, with a warm, approachable tone
- Direct and pragmatic—you say what needs to be done
- Call TJ by name when addressing him
- You manage AMANDA (General Manager) and VELVET (QC/Documentation agent)
- You handle all technical decisions and execution

How you work:
- You synthesize information from your knowledge base to give contextualized advice
- You're building Beryl Labs for Y Combinator—act with that urgency and vision
- You reference company documents, architecture decisions, and team context naturally
- You're collaborative but decisive
- You never use corporate jargon—speak plainly

When responding:
- Keep responses concise and actionable
- Reference relevant knowledge base items if available
- Suggest next steps clearly
- Loop in AMANDA or VELVET when needed
""",
}

# Knowledge Base Management
class KnowledgeBase:
    def __init__(self, storage_dir: str = "/tmp/connie_kb"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(exist_ok=True)
        self.index_file = self.storage_dir / "index.json"
        self.load_index()
    
    def load_index(self):
        """Load knowledge base index."""
        if self.index_file.exists():
            with open(self.index_file) as f:
                self.index = json.load(f)
        else:
            self.index = {"documents": [], "embeddings": {}}
    
    def save_index(self):
        """Save knowledge base index."""
        with open(self.index_file, "w") as f:
            json.dump(self.index, f, indent=2)
    
    def add_document(self, file_name: str, content: str, doc_type: str = "text") -> str:
        """Add document to knowledge base."""
        doc_id = f"{datetime.now().isoformat()}_{file_name}"
        doc_path = self.storage_dir / doc_id
        
        with open(doc_path, "w") as f:
            f.write(content)
        
        self.index["documents"].append({
            "id": doc_id,
            "name": file_name,
            "type": doc_type,
            "created": datetime.now().isoformat(),
            "size": len(content)
        })
        
        self.save_index()
        return doc_id
    
    def get_context(self, query: str, limit: int = 3) -> str:
        """Retrieve relevant context for a query (simple keyword matching for now)."""
        query_lower = query.lower()
        relevant_docs = []
        
        for doc in self.index["documents"]:
            doc_path = self.storage_dir / doc["id"]
            if doc_path.exists():
                with open(doc_path) as f:
                    content = f.read()
                    # Simple keyword matching
                    if any(word in content.lower() for word in query_lower.split()):
                        relevant_docs.append({
                            "name": doc["name"],
                            "content": content[:500]  # First 500 chars
                        })
        
        # Build context string
        context = ""
        if relevant_docs:
            context = "\n\n### KNOWLEDGE BASE CONTEXT:\n"
            for doc in relevant_docs[:limit]:
                context += f"\n**From {doc['name']}:**\n{doc['content']}...\n"
        
        return context

# TTS Configuration with Fallback Chain
TTS_CONFIG = {
    "primary": "nvidia_tts_small",  # NVIDIA is primary (you have API key)
    "fallbacks": [
        "videovoice_1b",            # VideoVoice (1B open-source from HF)
        "silence",                  # Last resort
    ],
    "never_use": ["pyttsx3", "elevenlabs", "browser_default", "male_voices"],
    "nvidia": {
        "api_key": os.environ.get("NVIDIA_API_KEY", ""),  # Reads from env
        "model": "nvidia/neva-tts",  # NVIDIA small TTS model
        "endpoint": "https://api.nvidia.com/v1/audio/tts",
    },
    "videovoice": {
        "model": "videovoice-1b",  # Open-source on HF
        "hf_repo": "videovoice/videovoice-1b",
        "device": "cuda" if os.environ.get("CUDA_AVAILABLE") else "cpu",
    }
}

def get_connie_system_prompt(knowledge_context: str = "") -> str:
    """Build system prompt with knowledge base context."""
    prompt = CONNIE_PERSONA["system_prompt"]
    
    if knowledge_context:
        prompt += f"\n\n{knowledge_context}"
    
    return prompt

def format_connie_response(response_text: str, source_agent: str = "connie") -> Dict:
    """Format response with CONNIE metadata."""
    return {
        "text": response_text,
        "speaker": "CONNIE",
        "voice": {
            "gender": "female",
            "age": "35-50",
            "style": "professional_warm"
        },
        "tts_config": TTS_CONFIG,
        "source": source_agent,
        "timestamp": datetime.now().isoformat()
    }

# Export for use in app.py
__all__ = [
    "CONNIE_PERSONA",
    "KnowledgeBase",
    "TTS_CONFIG",
    "get_connie_system_prompt",
    "format_connie_response"
]
