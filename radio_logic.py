import os
import random
import time
import json
import subprocess
import imageio_ffmpeg as ffmpeg
from threading import Thread, RLock
from queue import Queue, Full, Empty

# --- Constantes de Diretório ---
MUSIC_DIR = 'music'
JINGLES_DIR = 'jingles'
ADS_DIR = 'ads'
CONFIG_DIR = 'config'

SILENT_CHUNK = b'\xff\xfb\x90\x44' + b'\x00' * (4096 - 4)

# --- Função Auxiliar para drenar logs do FFmpeg ---
def drain_pipe(pipe):
    """Lê continuamente de um pipe e o LOGA, para evitar deadlocks e ver erros."""
    try:
        with pipe:
            for line in iter(pipe.readline, b''):
                error_line = line.decode('utf-8', errors='ignore').strip()
                if error_line:
                    #print(f"[FFMPEG_STDERR] {error_line}")
                    pass
    except Exception as e:
        #print(f"Erro ao drenar o pipe do FFmpeg: {e}")
        pass

class RadioStation:
    def __init__(self):
        self.lock = RLock()
        
        for d in [MUSIC_DIR, JINGLES_DIR, ADS_DIR, CONFIG_DIR]:
            os.makedirs(d, exist_ok=True)

        self.settings_file = os.path.join(CONFIG_DIR, 'settings.json')
        self.load_settings()

        # --- Lógica de Comutador de Fontes ---
        self.live_source_active = False
        self.autodj_queue = Queue(maxsize=128)
        self.live_queue = Queue(maxsize=128)
        
        # --- NOVO: Estado para os metadados ao vivo ---
        self.live_song_info = "AO VIVO"

        # --- Configurações e Estado (o resto do __init__) ---
        self.is_playing = True
        self.playback_mode = 'shuffle'
        self.jingle_interval = 3
        self.ad_interval = 10
        self.master_song_list = []
        self.master_jingle_list = []
        self.master_ad_list = []
        self.play_queue = []
        self.songs_since_jingle = 0
        self.songs_since_ad = 0
        self.last_jingle_index = -1
        self.last_ad_index = -1
        self.listeners = []
        self.current_item = None
        self.current_song_info = "Rádio iniciando..."
        
        self.reload_master_lists()

    # --- Métodos de Controle Ao Vivo ---
    def go_live(self):
        with self.lock:
            if not self.live_source_active:
                print(">>> MUDANÇA DE SINAL: ENTRANDO AO VIVO! <<<")
                self.live_source_active = True
                self.live_song_info = "AO VIVO - Aguardando metadados..." # Reseta ao entrar ao vivo
                while not self.live_queue.empty():
                    try: self.live_queue.get_nowait()
                    except Empty: break

    def end_live(self):
        with self.lock:
            if self.live_source_active:
                print(">>> MUDANÇA DE SINAL: SAINDO DO AR. RETOMANDO AUTO DJ. <<<")
                self.live_source_active = False

    # --- NOVO MÉTODO ---
    def update_live_metadata(self, song_name):
        """Atualiza o nome da música que está sendo tocada ao vivo."""
        with self.lock:
            # Limpa o nome da música recebido
            pretty_name = song_name.replace('+', ' ')
            self.live_song_info = pretty_name
            print(f"[METADADOS AO VIVO ATUALIZADOS] {pretty_name}")

    # --- Método MODIFICADO para ser um PRODUTOR ---
    def _auto_dj_thread(self):
        while True:
            while not self.is_playing:
                time.sleep(1)
            
            # Adicionado para que o Auto DJ espere se estiver ao vivo
            if self.live_source_active:
                time.sleep(1)
                continue

            item_type, filename = self._get_next_item()
            if not item_type:
                self.autodj_queue.put(SILENT_CHUNK)
                time.sleep(5)
                continue
            
            with self.lock:
                self.current_item = {'type': item_type, 'filename': filename}
                self.current_song_info = f"({item_type.upper()}) {filename}" if item_type != 'song' else filename
            
            dir_map = {'song': MUSIC_DIR, 'jingle': JINGLES_DIR, 'ad': ADS_DIR}
            item_path = os.path.join(dir_map[item_type], filename)
            
            if not os.path.exists(item_path):
                print(f"!!! AVISO: Arquivo não encontrado: {item_path}. Pulando.")
                self.reload_master_lists()
                continue

            print(f"--- [AutoDJ] Preparando: {self.current_song_info} ---")
            
            proc = None
            try:
                ffmpeg_exe = ffmpeg.get_ffmpeg_exe()
                ffmpeg_command = [ffmpeg_exe, '-re', '-i', item_path, '-vn', '-ar', '44100', '-ac', '2', '-b:a', '128k', '-f', 'mp3', 'pipe:1']
                proc = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                stderr_thread = Thread(target=drain_pipe, args=(proc.stderr,))
                stderr_thread.daemon = True
                stderr_thread.start()

                while self.is_playing and not self.live_source_active:
                    chunk = proc.stdout.read(4096)
                    if not chunk: break
                    self.autodj_queue.put(chunk)
                
                if proc.poll() is None:
                    proc.terminate() # Se o loop parou por entrar ao vivo, mata o FFmpeg
                
                return_code = proc.wait()
                stderr_thread.join()

                if return_code not in [0, -9]: # Ignora o código -9 (SIGTERM) que é normal
                    print(f"!!! AVISO: FFmpeg encerrou com código {return_code} para: {filename}. Ver logs.")
            
            except Exception as e:
                print(f"Erro no _auto_dj_thread: {e}")
                if proc and proc.poll() is None: proc.terminate()
            
            with self.lock:
                self.current_item = None

    # --- Thread MESTRA que transmite para os ouvintes (COMUTADOR) ---
    def _master_broadcast_thread(self):
        while True:
            chunk = None
            is_live_now = self.live_source_active
            try:
                if is_live_now:
                    chunk = self.live_queue.get(timeout=0.5)
                else:
                    chunk = self.autodj_queue.get(timeout=1)
            except Empty:
                chunk = SILENT_CHUNK
                if is_live_now:
                    print("[AVISO] Fonte Ao Vivo conectada, mas sem enviar dados (lag?).")
            
            if chunk:
                self._broadcast_chunk(chunk)
            time.sleep(0.001)

    # --- Método MODIFICADO para iniciar as duas threads ---
    def start(self):
        autodj_producer = Thread(target=self._auto_dj_thread, daemon=True)
        autodj_producer.start()
        master_broadcaster = Thread(target=self._master_broadcast_thread, daemon=True)
        master_broadcaster.start()
        print("Threads de Auto DJ (Produtor) e Transmissão Mestra (Comutador) iniciadas.")
        
    # --- MÉTODO MODIFICADO para retornar os metadados corretos ---
    def get_status(self):
        with self.lock:
            next_item = self._peek_next_item() if not self.live_source_active else None
            
            if self.live_source_active:
                current_playing_display = self.live_song_info
                current_item_obj = {'type': 'live', 'filename': self.live_song_info}
            else:
                current_playing_display = self.current_song_info
                current_item_obj = self.current_item
            
            return {
                "radio_name": self.radio_name, 
                "is_playing": self.is_playing, 
                "listeners": len(self.listeners), 
                "current_item": current_item_obj,
                "current_song_info_display": current_playing_display,
                "next_item": next_item, 
                "playback_mode": self.playback_mode, 
                "jingle_interval": self.jingle_interval, 
                "ad_interval": self.ad_interval
            }

    # O resto do arquivo (load_settings, save_order, etc.) permanece o mesmo...
    def load_settings(self):
        try:
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
            if not os.path.exists(self.settings_file): raise FileNotFoundError("Arquivo de configurações não encontrado.")
            with open(self.settings_file, 'r', encoding='utf-8') as f: settings = json.load(f); self.radio_name = settings.get('radio_name', 'Rádio Python')
        except (FileNotFoundError, json.JSONDecodeError) as e: print(f"[INFO] Criando novo arquivo de configurações ({e})"); self.radio_name = 'Rádio Python'; self.save_settings()
    def save_settings(self):
        with self.lock:
            settings = {'radio_name': self.radio_name}
            with open(self.settings_file, 'w', encoding='utf-8') as f: json.dump(settings, f, indent=4)
            print("Configurações salvas.")
    def set_radio_name(self, name):
        with self.lock: self.radio_name = name; self.save_settings()
    def start_playback(self):
        with self.lock:
            if not self.is_playing: self.is_playing = True; print(">>> COMANDO: Transmissão iniciada pelo painel.")
    def stop_playback(self):
        with self.lock:
            if self.is_playing: self.is_playing = False; print(">>> COMANDO: Transmissão parada pelo painel.")
    def _load_order(self, order_file_path, available_files):
        if not os.path.exists(order_file_path): return available_files
        with open(order_file_path, 'r', encoding='utf-8') as f: ordered_filenames = [line.strip() for line in f]
        available_set = set(available_files); final_order = [f for f in ordered_filenames if f in available_set]; new_files = [f for f in available_files if f not in final_order]; final_order.extend(new_files)
        return final_order
    def save_order(self, file_type, ordered_filenames):
        with self.lock:
            order_file_path = os.path.join(CONFIG_DIR, f"{file_type}_order.txt")
            with open(order_file_path, 'w', encoding='utf-8') as f:
                for filename in ordered_filenames: f.write(f"{filename}\n")
            self.reload_master_lists(list_type=file_type)
    def reload_master_lists(self, list_type='all'):
        with self.lock:
            if list_type in ['all', 'songs']: available = self._scan_directory(MUSIC_DIR); self.master_song_list = self._load_order(os.path.join(CONFIG_DIR, 'songs_order.txt'), available)
            if list_type in ['all', 'jingles']: available = self._scan_directory(JINGLES_DIR); self.master_jingle_list = self._load_order(os.path.join(CONFIG_DIR, 'jingles_order.txt'), available)
            if list_type in ['all', 'ads']: available = self._scan_directory(ADS_DIR); self.master_ad_list = self._load_order(os.path.join(CONFIG_DIR, 'ads_order.txt'), available)
            print("Listas mestras recarregadas com ordem customizada.")
    def _scan_directory(self, path): return [f for f in os.listdir(path) if f.endswith('.mp3')]
    def _build_play_queue(self):
        temp_song_list = self.master_song_list.copy();
        if not temp_song_list: return
        if self.playback_mode == 'shuffle': random.shuffle(temp_song_list)
        self.play_queue.extend(temp_song_list)
    def set_playback_mode(self, mode):
        with self.lock:
            if mode in ['shuffle', 'sequential']: self.playback_mode = mode
    def set_intervals(self, jingle_interval, ad_interval):
        with self.lock: self.jingle_interval = int(jingle_interval); self.ad_interval = int(ad_interval)
    def _get_next_item(self):
        with self.lock:
            if self.jingle_interval > 0 and self.songs_since_jingle >= self.jingle_interval and self.master_jingle_list: self.songs_since_jingle = 0; self.last_jingle_index = (self.last_jingle_index + 1) % len(self.master_jingle_list); return ('jingle', self.master_jingle_list[self.last_jingle_index])
            if self.ad_interval > 0 and self.songs_since_ad >= self.ad_interval and self.master_ad_list: self.songs_since_ad = 0; self.last_ad_index = (self.last_ad_index + 1) % len(self.master_ad_list); return ('ad', self.master_ad_list[self.last_ad_index])
            if not self.play_queue: self._build_play_queue();
            if not self.play_queue: return (None, None)
            self.songs_since_jingle += 1; self.songs_since_ad += 1
            return ('song', self.play_queue.pop(0))
    def _peek_next_item(self):
        with self.lock:
            next_songs_since_jingle = self.songs_since_jingle + 1; next_songs_since_ad = self.songs_since_ad + 1
            if self.jingle_interval > 0 and next_songs_since_jingle > self.jingle_interval and self.master_jingle_list: next_index = (self.last_jingle_index + 1) % len(self.master_jingle_list); return {'type': 'jingle', 'filename': self.master_jingle_list[next_index]}
            if self.ad_interval > 0 and next_songs_since_ad > self.ad_interval and self.master_ad_list: next_index = (self.last_ad_index + 1) % len(self.master_ad_list); return {'type': 'ad', 'filename': self.master_ad_list[next_index]}
            if self.play_queue: return {'type': 'song', 'filename': self.play_queue[0]}
            if self.master_song_list: return {'type': 'song', 'filename': '(Próxima aleatória...)' if self.playback_mode == 'shuffle' else self.master_song_list[0]}
            return None
    def add_listener(self):
        queue = Queue(maxsize=512);
        with self.lock: self.listeners.append(queue)
        return queue
    def remove_listener(self, queue):
        with self.lock:
            if queue in self.listeners: self.listeners.remove(queue)
    def _broadcast_chunk(self, chunk):
        with self.lock:
            for queue in self.listeners:
                try: queue.put_nowait(chunk)
                except Full: pass