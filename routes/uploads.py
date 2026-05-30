# -*- coding: utf-8 -*-
"""
File upload and knowledge base management.
"""
import os
import json
import asyncio
from aiohttp import web
from PyPDF2 import PdfReader

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge_base")

async def upload_file(request: web.Request) -> web.Response:
    """Handle multipart file upload."""
    reader = await request.multipart()
    
    # We only care about the first file part for now
    field = await reader.next()
    if not field or field.name != 'file':
        return web.json_response({"ok": False, "error": "No file field found"}, status=400)
    
    filename = field.filename
    if not filename:
        return web.json_response({"ok": False, "error": "No filename found"}, status=400)
    
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    # Save the file
    size = 0
    with open(file_path, 'wb') as f:
        while True:
            chunk = await field.read_chunk()  # 8192 bytes by default
            if not chunk:
                break
            size += len(chunk)
            f.write(chunk)
    
    print(f"📁 [UPLOAD]: Received {filename} ({size} bytes)")
    
    # Optional: Extract text if it's a PDF or TXT
    extracted_text = ""
    if filename.lower().endswith(".pdf"):
        try:
            pdf = PdfReader(file_path)
            for page in pdf.pages:
                extracted_text += page.extract_text() + "\n"
        except Exception as e:
            print(f"⚠️ [EXTRACT ERROR]: {e}")
    elif filename.lower().endswith((".txt", ".md", ".json", ".csv")):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                extracted_text = f.read()
        except Exception as e:
            print(f"⚠️ [EXTRACT ERROR]: {e}")

    # Store extracted text in a companion .txt file if extraction was successful
    if extracted_text:
        text_filename = filename + ".extracted.txt"
        with open(os.path.join(UPLOAD_DIR, text_filename), 'w', encoding='utf-8') as f:
            f.write(extracted_text)
        print(f"📖 [EXTRACTED]: Saved text for {filename}")

    return web.json_response({
        "ok": True, 
        "filename": filename, 
        "size": size,
        "extracted": bool(extracted_text)
    })

async def list_files(request: web.Request) -> web.Response:
    """List uploaded files in the knowledge base."""
    files = []
    for f in os.listdir(UPLOAD_DIR):
        if f == ".gitkeep" or f.endswith(".extracted.txt"):
            continue
        
        full_path = os.path.join(UPLOAD_DIR, f)
        files.append({
            "name": f,
            "size": os.path.getsize(full_path),
            "time": os.path.getmtime(full_path),
            "has_text": os.path.exists(full_path + ".extracted.txt")
        })
    
    return web.json_response({"ok": True, "files": sorted(files, key=lambda x: x['time'], reverse=True)})

async def delete_file(request: web.Request) -> web.Response:
    """Delete a file and its extracted text."""
    data = await request.json()
    filename = data.get("filename")
    if not filename:
        return web.json_response({"ok": False, "error": "No filename provided"}, status=400)
    
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        # Remove extracted text if exists
        text_path = file_path + ".extracted.txt"
        if os.path.exists(text_path):
            os.remove(text_path)
        return web.json_response({"ok": True})
    
    return web.json_response({"ok": False, "error": "File not found"}, status=404)
