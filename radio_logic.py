import os
import random
import time
import json
import subprocess
import imageio_ffmpeg as ffmpeg
from threading import Thread, RLock
from queue import Queue, Full

# --- Constantes de Diretório ---
MUSIC_DIR = 'music'
JINGLES_DIR = 'jingles'
ADS_DIR = 'ads'
CONFIG_DIR = 'config'

SILENT_CHUNK = b'\xff\xfb\x90\x44' + b'\x00' * (4096 - 4)

# --- NOVA FUNÇÃO AUXILIAR ---
def drain_pipe(pipe, log_prefix):
    """Lê continuamente de um pipe e imprime, para evitar deadlocks."""
    try:
        with pipe:
            for line in iter(pipe.readline, b''):
                # Imprime os logs do ffmpeg para depuração, se necessário
                # print(f"[{log_prefix}] {line.decode('utf-8', errors='ignore').strip()}")
                pass # Apenas consumir a linha já é o suficiente
    except Exception as e:
        print(f"Erro ao drenar o pipe {log_prefix}: {e}")

class RadioStation:
    def __init__(self):
        self.lock = RLock()
        
        for d in [MUSIC_DIR, JINGLES_DIR, ADS_DIR, CONFIG_DIR]:
            try:
                if not os.path.exists(d): os.makedirs(d)
            except Exception as e:
                print(f"Erro ao criar diretório '{d}': {e}")

        self.settings_file = os.path.join(CONFIG_DIR, 'settings.json')
        self.load_settings()

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

    # Todas as funções até _auto_dj_thread permanecem as mesmas
    def load_settings(self):
        try:
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
            if not os.path.exists(self.settings_file):
                raise FileNotFoundError("Arquivo de configurações não encontrado.")
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                self.radio_name = settings.get('radio_name', 'Rádio Python')
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[INFO] Criando novo arquivo de configurações ({e})")
            self.radio_name = 'Rádio Python'
            self.save_settings()

    def save_settings(self):
        with self.lock:
            settings = {'radio_name': self.radio_name}
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
            print("Configurações salvas.")

    def set_radio_name(self, name):
        with self.lock:
            self.radio_name = name
            self.save_settings()

    def start_playback(self):
        with self.lock:
            if not self.is_playing:
                self.is_playing = True
                print(">>> COMANDO: Transmissão iniciada pelo painel.")
    
    def stop_playback(self):
        with self.lock:
            if self.is_playing:
                self.is_playing = False
                print(">>> COMANDO: Transmissão parada pelo painel.")

    def _load_order(self, order_file_path, available_files):
        if not os.path.exists(order_file_path): return available_files
        with open(order_file_path, 'r', encoding='utf-8') as f: ordered_filenames = [line.strip() for line in f]
        available_set = set(available_files)
        final_order = [f for f in ordered_filenames if f in available_set]
        new_files = [f for f in available_files if f not in final_order]
        final_order.extend(new_files)
        return final_order

    def save_order(self, file_type, ordered_filenames):
        with self.lock:
            order_file_path = os.path.join(CONFIG_DIR, f"{file_type}_order.txt")
            with open(order_file_path, 'w', encoding='utf-8') as f:
                for filename in ordered_filenames: f.write(f"{filename}\n")
            self.reload_master_lists(list_type=file_type)
            print(f"Nova ordem para '{file_type}' foi salva.")

    def reload_master_lists(self, list_type='all'):
        with self.lock:
            if list_type in ['all', 'songs']:
                available = self._scan_directory(MUSIC_DIR)
                self.master_song_list = self._load_order(os.path.join(CONFIG_DIR, 'songs_order.txt'), available)
            if list_type in ['all', 'jingles']:
                available = self._scan_directory(JINGLES_DIR)
                self.master_jingle_list = self._load_order(os.path.join(CONFIG_DIR, 'jingles_order.txt'), available)
            if list_type in ['all', 'ads']:
                available = self._scan_directory(ADS_DIR)
                self.master_ad_list = self._load_order(os.path.join(CONFIG_DIR, 'ads_order.txt'), available)
            print("Listas mestras recarregadas com ordem customizada.")

    def _scan_directory(self, path): return [f for f in os.listdir(path) if f.endswith('.mp3')]

    def _build_play_queue(self):
        temp_song_list = self.master_song_list.copy()
        if not temp_song_list: return
        if self.playback_mode == 'shuffle': random.shuffle(temp_song_list)
        self.play_queue.extend(temp_song_list)

    def set_playback_mode(self, mode):
        with self.lock:
            if mode in ['shuffle', 'sequential']: self.playback_mode = mode

    def set_intervals(self, jingle_interval, ad_interval):
        with self.lock:
            self.jingle_interval = int(jingle_interval)
            self.ad_interval = int(ad_interval)

    def _get_next_item(self):
        with self.lock:
            if self.jingle_interval > 0 and self.songs_since_jingle >= self.jingle_interval and self.master_jingle_list:
                self.songs_since_jingle = 0
                self.last_jingle_index = (self.last_jingle_index + 1) % len(self.master_jingle_list)
                return ('jingle', self.master_jingle_list[self.last_jingle_index])
            if self.ad_interval > 0 and self.songs_since_ad >= self.ad_interval and self.master_ad_list:
                self.songs_since_ad = 0
                self.last_ad_index = (self.last_ad_index + 1) % len(self.master_ad_list)
                return ('ad', self.master_ad_list[self.last_ad_index])
            if not self.play_queue:
                self._build_play_queue()
                if not self.play_queue: return (None, None)
            self.songs_since_jingle += 1
            self.songs_since_ad += 1
            return ('song', self.play_queue.pop(0))

    def _peek_next_item(self):
        with self.lock:
            # ... (esta função continua a mesma) ...
            next_songs_since_jingle = self.songs_since_jingle + 1
            next_songs_since_ad = self.songs_since_ad + 1
            if self.jingle_interval > 0 and next_songs_since_jingle > self.jingle_interval and self.master_jingle_list:
                next_index = (self.last_jingle_index + 1) % len(self.master_jingle_list)
                return {'type': 'jingle', 'filename': self.master_jingle_list[next_index]}
            if self.ad_interval > 0 and next_songs_since_ad > self.ad_interval and self.master_ad_list:
                next_index = (self.last_ad_index + 1) % len(self.master_ad_list)
                return {'type': 'ad', 'filename': self.master_ad_list[next_index]}
            if self.play_queue:
                return {'type': 'song', 'filename': self.play_queue[0]}
            if self.master_song_list:
                return {'type': 'song', 'filename': '(Próxima aleatória...)' if self.playback_mode == 'shuffle' else self.master_song_list[0]}
            return None

    # --- MÉTODO MODIFICADO PARA EVITAR DEADLOCKS ---
    def _auto_dj_thread(self):
        while True:
            while not self.is_playing:
                self.current_item = None; self.current_song_info = "Transmissão Pausada"
                self._broadcast_chunk(SILENT_CHUNK); time.sleep(1)
            
            item_type, filename = self._get_next_item()
            if not item_type:
                self.current_item = None; self.current_song_info = "Nenhuma música na pasta..."
                self._broadcast_chunk(SILENT_CHUNK); time.sleep(5)
                continue
            
            self.current_item = {'type': item_type, 'filename': filename}
            self.current_song_info = f"({item_type.upper()}) {filename}" if item_type != 'song' else filename
            dir_map = {'song': MUSIC_DIR, 'jingle': JINGLES_DIR, 'ad': ADS_DIR}
            item_path = os.path.join(dir_map[item_type], filename)
            
            print(f"--- Transcodificando e tocando agora: {self.current_song_info} ---")
            
            proc = None
            try:
                ffmpeg_exe = ffmpeg.get_ffmpeg_exe()
                ffmpeg_command = [
                    ffmpeg_exe, '-re', '-i', item_path, '-vn', '-ar', '44100', '-ac', '2',
                    '-b:a', '128k', '-f', 'mp3', 'pipe:1'
                ]

                proc = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                # --- A SOLUÇÃO: Thread para drenar o stderr ---
                stderr_thread = Thread(target=drain_pipe, args=(proc.stderr, "FFMPEG_LOG"))
                stderr_thread.daemon = True
                stderr_thread.start()

                while self.is_playing:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    self._broadcast_chunk(chunk)
                
                return_code = proc.wait()
                if return_code != 0:
                    print(f"!!! AVISO: FFmpeg encerrou com código {return_code} para o arquivo: {filename}. Pulando para o próximo.")
            
            except Exception as e:
                print(f"Erro inesperado durante a transcodificação: {e}")
                if proc and proc.poll() is None:
                    proc.terminate()
            
            self._broadcast_chunk(SILENT_CHUNK)
            self.current_item = None
    
    # O resto do arquivo continua igual...
    def add_listener(self):
        queue = Queue(maxsize=512)
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
    def get_status(self):
        with self.lock:
            next_item = self._peek_next_item()
            return {"radio_name": self.radio_name, "is_playing": self.is_playing, "listeners": len(self.listeners), "current_item": self.current_item, "next_item": next_item, "playback_mode": self.playback_mode, "jingle_interval": self.jingle_interval, "ad_interval": self.ad_interval, "queue_size": len(self.play_queue)}
    def start(self):
        Thread(target=self._auto_dj_thread, daemon=True).start()
        print("Thread do Auto DJ (PRO) iniciada.")