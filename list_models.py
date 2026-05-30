import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"

print(f"Fetching models from: {url.replace(api_key, 'REDACTED')}")
response = requests.get(url)

if response.status_code == 200:
    models = response.json().get('models', [])
    print(f"\nFound {len(models)} models. Searching for 'bidiGenerateContent' support...\n")
    
    found_any = False
    for model in models:
        methods = model.get('supportedGenerationMethods', [])
        if 'bidiGenerateContent' in methods:
            print(f"✅ MODEL: {model['name']}")
            print(f"   DisplayName: {model['displayName']}")
            print(f"   Methods: {methods}")
            print("-" * 30)
            found_any = True
            
    if not found_any:
        print("❌ CRITICAL: No models found with 'bidiGenerateContent' support for this API key.")
        print("This might mean the API key doesn't have Multimodal Live permissions yet.")
else:
    print(f"Error: {response.status_code}")
    print(response.text)
