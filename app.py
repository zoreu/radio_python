# -*- coding: utf-8 -*-
import sys
import os
import threading
import time
import requests
import uvicorn
import yt_dlp
import secrets
import shutil
import base64
import asyncio
from urllib.parse import urlparse, unquote_plus # Adiciona unquote_plus
from fastapi import FastAPI, Request, Response, Form, File, UploadFile, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from werkzeug.utils import secure_filename
import logging

PORT_LIVE_TEMP = 8080

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Importa a nossa lógica de rádio
from radio_logic import RadioStation, MUSIC_DIR, JINGLES_DIR, ADS_DIR

# --- INICIALIZAÇÃO DO APP FASTAPI (COMO UM OBJETO) ---
app = FastAPI(title="Rádio Python PRO")
os.makedirs('static', exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
radio = RadioStation()

# --- Autenticação NATIVA do FastAPI ---
security = HTTPBasic()
live_security = HTTPBasic(realm="Live Stream")

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    """Verifica as credenciais do painel de administração."""
    is_user_ok = secrets.compare_digest(credentials.username, radio.admin_user)
    is_pass_ok = secrets.compare_digest(credentials.password, radio.admin_password)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais incorretas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def get_current_live_user(credentials: HTTPBasicCredentials = Depends(live_security)):
    """Verifica as credenciais do DJ ao vivo."""
    is_user_ok = secrets.compare_digest(credentials.username, radio.live_user)
    is_pass_ok = secrets.compare_digest(credentials.password, radio.live_password)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais incorretas para transmissão ao vivo",
            headers={"WWW-Authenticate": "Basic realm='Live Stream'"},
        )
    return credentials.username

# --- Filtro Jinja2 e Evento Startup ---
def format_filename(filename: str):
    if not filename: return ""
    return os.path.splitext(filename)[0].replace('_', ' ')
templates.env.filters['prettify'] = format_filename

@app.on_event("startup")
def startup_event():
    logger.info(">>> Evento Startup do FastAPI: Iniciando threads da rádio... <<<")
    radio.start()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("player.html", {"request": request, "radio_name": radio.radio_name})

@app.get("/player_embed", response_class=HTMLResponse)
async def player_embed(request: Request):
    return templates.TemplateResponse("embed.html", {"request": request, "radio_name": radio.radio_name})

@app.get("/stream")
async def audio_stream():
    def stream_generator():
        queue = radio.add_listener()
        try:
            while True: yield queue.get()
        finally: radio.remove_listener(queue)
    return StreamingResponse(stream_generator(), media_type="audio/mpeg", headers={'Cache-Control': 'no-cache'})

@app.get("/status")
async def public_status():
    status = radio.get_status()
    return JSONResponse(content={
        "radio_name": status.get("radio_name"),
        "current_song_info_display": status.get("current_song_info_display"),
        "current_cover_url": status.get("current_cover_url")
    })

@app.get("/now_playing")
async def now_playing():
    # Agora retorna a informação correta, seja do Auto DJ ou do Ao Vivo
    status = radio.get_status()
    return Response(content=status['current_song_info_display'], media_type="text/plain")
    
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, user: str = Depends(get_current_user)):
    status = radio.get_status()
    context = {"request": request, "status": status, "songs": radio.master_song_list, "jingles": radio.master_jingle_list, "ads": radio.master_ad_list, 'port_live': PORT_LIVE_TEMP}
    return templates.TemplateResponse("admin.html", context)

@app.get("/admin/status")
async def admin_status(user: str = Depends(get_current_user)):
    status = radio.get_status()
    status['is_live'] = radio.live_source_active
    return JSONResponse(content=status)

@app.post("/admin/upload")
async def upload_file_route(type: str = Form(...), file: UploadFile = File(...), user: str = Depends(get_current_user)):
    if type in ['song', 'jingle', 'ad'] and file.filename.endswith('.mp3'):
        filename = secure_filename(file.filename)
        dir_map = {'song': MUSIC_DIR, 'jingle': JINGLES_DIR, 'ad': ADS_DIR}
        save_path = os.path.join(dir_map[type], filename)
        with open(save_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        radio.reload_master_lists(list_type=f"{type}s")
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/delete")
async def delete_file_route(type: str = Form(...), filename: str = Form(...), user: str = Depends(get_current_user)):
    dir_map = {'song': MUSIC_DIR, 'jingle': JINGLES_DIR, 'ad': ADS_DIR}
    file_path = os.path.join(dir_map[type], filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        radio.reload_master_lists(list_type=f"{type}s")
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/settings/playback")
async def update_playback_settings(playback_mode: str = Form(...), jingle_interval: int = Form(...), ad_interval: int = Form(...), user: str = Depends(get_current_user)):
    radio.set_playback_mode(playback_mode); radio.set_intervals(jingle_interval, ad_interval)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/settings/general")
async def update_general_settings(radio_name: str = Form(...), user: str = Depends(get_current_user)):
    if radio_name: radio.set_radio_name(radio_name)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/settings/live")
async def update_live_settings(live_user: str = Form(...), live_password: str = Form(None), user: str = Depends(get_current_user)):
    if live_password == "": live_password = None
    radio.set_live_credentials(live_user, live_password)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/settings/admin_credentials")
async def update_admin_credentials(admin_user: str = Form(...), admin_password: str = Form(None), user: str = Depends(get_current_user)):
    if admin_password == "": admin_password = None
    radio.set_admin_credentials(admin_user, admin_password)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/playback")
async def control_playback(action: str = Form(...), user: str = Depends(get_current_user)):
    if action == 'stop':
        radio.stop_playback()
    elif action == 'start':
        radio.start_playback()
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/reorder")
async def reorder_files(request: Request, user: str = Depends(get_current_user)):
    data = await request.json()
    file_type = data.get('type')
    order = data.get('order')
    radio.save_order(file_type, order)
    return JSONResponse(content={"status": "success"})

@app.post("/admin/search")
async def search_youtube(query: str = Form(...), user: str = Depends(get_current_user)):
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'default_search': 'ytsearch5',
        'quiet': True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(query, download=False)
            videos = result.get('entries', [])
            search_results = [
                {
                    'id': v.get('id'),
                    'title': v.get('title'),
                    'thumbnail': v.get('thumbnail'),
                    'duration': time.strftime('%M:%S', time.gmtime(v.get('duration', 0)))
                } for v in videos
            ]
            return JSONResponse(content=search_results)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.post("/admin/download")
async def download_youtube(video_id: str = Form(...), user: str = Depends(get_current_user)):
    def download_in_background(vid):
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(MUSIC_DIR, '%(title)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128'
            }],
            'noplaylist': True,
            'quiet': True
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f'https://www.youtube.com/watch?v={vid}'])
            radio.reload_master_lists(list_type='songs')
        except Exception as e:
            print(f"Erro no download do vídeo {vid}: {e}")
    thread = threading.Thread(target=download_in_background, args=(video_id,))
    thread.daemon = True
    thread.start()
    return JSONResponse(content={"status": "success"})

@app.post("/admin/download_from_url")
async def download_from_url(type: str = Form(...), url: str = Form(...), user: str = Depends(get_current_user)):
    def download_task(target_url, f_type):
        try:
            filename = os.path.basename(urlparse(target_url).path) or f"download_{int(time.time())}.mp3"
            filename = secure_filename(filename)
            dir_map = {'jingle': JINGLES_DIR, 'ad': ADS_DIR}
            save_path = os.path.join(dir_map[f_type], filename)
            with requests.get(target_url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(save_path, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
            radio.reload_master_lists(list_type=f"{f_type}s")
        except Exception as e:
            print(f"Erro ao baixar da URL {target_url}: {e}")
    thread = threading.Thread(target=download_task, args=(url, type))
    thread.daemon = True
    thread.start()
    return RedirectResponse(url="/admin", status_code=303)

# --- Rotas Falsas para o RadioBOSS (evita erros 404 no log) ---
@app.get("/admin/listclients")
async def list_clients(mount: str, user: str = Depends(get_current_live_user)):
    return Response(content="<icestats></icestats>", media_type="application/xml")

@app.get("/admin/metadata")
async def update_metadata(mode: str, mount: str, song: str, user: str = Depends(get_current_live_user)):
    # O Radio Boss envia o nome da música aqui.
    # O parâmetro 'user' foi removido pois esta rota pode não ser autenticada por alguns clientes
    if mount == '/live' and mode == 'updinfo':
        # 'unquote_plus' decodifica caracteres especiais como '%20' para espaços
        song_name = unquote_plus(song)
        radio.update_live_metadata(song_name)
    return Response(content="Metadata updated.", media_type="text/plain")

@app.post("/admin/settings/general")
async def update_general_settings(radio_name: str = Form(...), user: str = Depends(get_current_user)):
    if radio_name:
        radio.set_radio_name(radio_name)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/settings/live")
async def update_live_settings(live_user: str = Form(...), live_password: str = Form(None), user: str = Depends(get_current_user)):
    if live_password == "": live_password = None
    radio.set_live_credentials(live_user, live_password)
    return RedirectResponse(url="/admin", status_code=303)

# --- LÓGICA DO SERVIDOR HÍBRIDO (O "GUARDA DE TRÂNSITO") ---

# 1. Manipulador Especializado para o Ao Vivo
async def handle_live_source(reader, writer):
    addr = writer.get_extra_info('peername')
    logger.info(f"[{addr}] Roteado para o manipulador Ao Vivo.")
    try:
        header_raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=10.0)
        headers = header_raw.decode('latin-1').split('\r\n')
        
        ice_name_header = next((h for h in headers if h.lower().startswith('ice-name: ')), None)
        if ice_name_header: radio.update_live_metadata(ice_name_header.split(':', 1)[1].strip())

        auth_header = next((h for h in headers if h.lower().startswith('authorization: basic ')), None)
        if not auth_header: raise Exception("Autenticação não fornecida")
        
        encoded_creds = auth_header.split(' ')[2]
        decoded_creds = base64.b64decode(encoded_creds).decode('utf-8')
        username, password = decoded_creds.split(':', 1)
        if not (secrets.compare_digest(username, radio.live_user) and secrets.compare_digest(password, radio.live_password)):
            raise Exception("Credenciais inválidas")
            
        writer.write(b'HTTP/1.0 200 OK\r\nIcecast-Auth: 1\r\n\r\n')
        await writer.drain()
        radio.go_live()
        
        # O corpo que veio junto com os cabeçalhos já foi lido por readuntil,
        # então o reader está pronto para o stream de áudio.
        while True:
            chunk = await reader.read(4096)
            if not chunk: break
            radio.live_queue.put(chunk)
    except Exception as e:
        logger.error(f"[Live Handler {addr}] Erro: {e}")
    finally:
        radio.end_live()
        if not writer.is_closing(): writer.close(); await writer.wait_closed()
        logger.info(f"[*] Conexão Ao Vivo de {addr} encerrada.")

# 2. Proxy Transparente para o FastAPI
async def proxy_to_fastapi(reader, writer, initial_data, internal_port: int):
    addr = writer.get_extra_info('peername')
    logger.info(f"[{addr}] Roteando para o servidor FastAPI interno.")
    try:
        # Conecta-se ao servidor Uvicorn que está rodando internamente
        fastapi_reader, fastapi_writer = await asyncio.open_connection('127.0.0.1', internal_port)
        
        # Envia os dados que já lemos
        fastapi_writer.write(initial_data)
        await fastapi_writer.drain()
        
        # Inicia a retransmissão de dados nos dois sentidos
        async def forward(src_reader, dst_writer):
            while not src_reader.at_eof():
                data = await src_reader.read(4096)
                if not data: break
                dst_writer.write(data)
                await dst_writer.drain()
            # Garante que o fim da conexão seja propagado
            if not dst_writer.is_closing():
                dst_writer.close()
                await dst_writer.wait_closed()

        await asyncio.gather(
            forward(reader, fastapi_writer),
            forward(fastapi_reader, writer)
        )
    except Exception as e:
        logger.error(f"Erro no proxy para o FastAPI de {addr}: {e}")
    finally:
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()

# 3. O Roteador Principal
async def connection_handler(reader, writer, internal_port: int):
    initial_data = b''
    try:
        # Espia a primeira parte da requisição.
        # read(n) é mais seguro que readline() para dados brutos.
        initial_data = await asyncio.wait_for(reader.read(2048), timeout=5.0)
        if not initial_data: return

        first_line = initial_data.split(b'\r\n', 1)[0].decode('latin-1', errors='ignore')

        if first_line.startswith('SOURCE /live') or first_line.startswith('PUT /live'):
            # Cria um novo "leitor" que começa com os dados que já pegamos
            new_reader = asyncio.StreamReader()
            new_reader.feed_data(initial_data)
            
            # Encaminha o resto dos dados do leitor original para o novo
            async def feed_new_reader():
                while True:
                    try:
                        data = await reader.read(4096)
                        if not data:
                            new_reader.feed_eof()
                            break
                        new_reader.feed_data(data)
                    except:
                        new_reader.feed_eof()
                        break
            
            asyncio.create_task(feed_new_reader())
            # Chama o handler do ao vivo com o NOVO leitor
            await handle_live_source(new_reader, writer)
        else:
            # É uma requisição web normal, passa para o proxy
            await proxy_to_fastapi(reader, writer, initial_data, internal_port)
            
    except asyncio.TimeoutError: pass
    except Exception as e:
        logger.error(f"Erro no handler de conexão: {e}")
    finally:
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()

# 4. Orquestrador de Inicialização
async def main_loop(public_port):
    global PORT_LIVE_TEMP
    PORT_LIVE_TEMP = public_port
    internal_port = public_port + 1
    # Inicia o servidor Uvicorn para o FastAPI em uma porta interna e em background
    config = uvicorn.Config(app, host="127.0.0.1", port=internal_port, log_level="info")
    server = uvicorn.Server(config)
    uvicorn_thread = threading.Thread(target=server.run)
    uvicorn_thread.daemon = True
    uvicorn_thread.start()
    
    # Aguarda um segundo para o Uvicorn iniciar
    await asyncio.sleep(1)

    # Inicia o nosso "guarda de trânsito" na porta pública
    public_server = await asyncio.start_server(
        lambda r, w: connection_handler(r, w, internal_port=internal_port), 
        "0.0.0.0", 
        public_port
    )
    
    logger.info(f"Servidor Híbrido rodando na porta pública {public_port}")
    logger.info(f"FastAPI interno rodando na porta {public_port + 1}")
    
    async with public_server:
        await public_server.serve_forever()


# --- INICIALIZAÇÃO UNIVERSAL ---
if __name__ == '__main__':
    port = 8000    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        # if a in ("--host", "-h"):
        #     if i+1 < len(args):
        #         host = args[i+1]
        #         i += 2
        #     else:
        #         raise SystemExit("Erro: --host precisa de um valor")
        if a in ("--port", "-p"):
            if i+1 < len(args):
                try:
                    port = int(args[i+1])
                except ValueError:
                    raise SystemExit("Erro: --port precisa ser um número")
                i += 2
            else:
                raise SystemExit("Erro: --port precisa de um valor")
        else:
            # ignora ou trate outros args aqui
            i += 1    

    print("="*50)
    print(">>> Iniciando Rádio PRO (Servidor Híbrido FastAPI + AsyncIO) <<<")
    print(f"Servidor público escutando em: http://0.0.0.0:{port}")
    print(f"Painel Admin em: http://127.0.0.1:{port}/admin")
    print("="*50)

    try:
        asyncio.run(main_loop(port))
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
