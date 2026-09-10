#!/bin/bash

cd /home/hunt/Downloads/THECODE/connie-crane

echo "=== 1. Checking how voice_engine.py generates audio ==="
grep -n -A 35 'def generate' voice_engine.py || grep -n -A 35 'def synthesize' voice_engine.py || true

echo -e "\n=== 2. Checking Python environment for vibevoice or VibeVoice source ==="
python3 -c "
for mod in ['vibevoice', 'VibeVoice', 'torch', 'soundfile']:
    try:
        __import__(mod)
        print(f'{mod}: AVAILABLE')
    except ImportError as e:
        print(f'{mod}: NOT FOUND ({e})')
"

echo -e "\n=== 3. Searching for cloned VibeVoice repo/source directories ==="
find /home/hunt/ -maxdepth 3 -type d -iname "*vibevoice*" 2>/dev/null || true

