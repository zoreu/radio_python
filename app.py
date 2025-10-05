# app.py

import sys
import os
from flask import Flask, Response, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
from waitress import serve
from flask_httpauth import HTTPBasicAuth
from jinja2.exceptions import TemplateNotFound # Importe a exceção específica
# https://lmarena.ai/c/50d98abe-5b6e-4d46-8190-85c0f587c0fe

# Importa a nossa lógica de rádio
from radio_logic import RadioStation, MUSIC_DIR, JINGLES_DIR, ADS_DIR

app = Flask(__name__)
# Chave secreta para mensagens flash (necessário para feedback ao usuário)
app.secret_key = 'secret123456' 
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
auth = HTTPBasicAuth()

# --- NOVO: FILTRO CUSTOMIZADO PARA FORMATAR NOMES DE ARQUIVOS ---
def format_filename(filename):
    """Remove a extensão .mp3 e substitui underscores por espaços."""
    # Remove a extensão do arquivo, independentemente de ser .mp3, .MP3, etc.
    name_without_extension = os.path.splitext(filename)[0]
    # Substitui underscores por espaços
    return name_without_extension.replace('_', ' ')

# Registra a função como um filtro no Jinja2 com o nome 'prettify'
app.jinja_env.filters['prettify'] = format_filename


# --- Configuração de Usuário e Senha ---
# Em um projeto real, isso viria de um banco de dados ou arquivo de configuração!
users = {
    "admin": "senha123"
}

@auth.verify_password
def verify_password(username, password):
    if username in users and users[username] == password:
        return username

# --- Instância Global da Rádio ---
# Esta é a única instância que controlará tudo.
radio = RadioStation()

# --- Rotas Públicas (para os ouvintes) ---

@app.route('/')
def index():
    """Renderiza a página principal do player para os ouvintes."""
    return render_template('player.html', radio_name=radio.radio_name)

@app.route('/player_embed')
def player_embed():
    """Renderiza o player minimalista para ser incorporado via iframe."""
    return render_template('embed.html', radio_name=radio.radio_name)

@app.route('/stream')
def audio_stream():
    def stream_generator():
        queue = radio.add_listener()
        try:
            while True:
                yield queue.get()
        finally:
            radio.remove_listener(queue)

    response = Response(stream_generator(), mimetype='audio/mpeg')
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/now_playing')
def now_playing():
    return radio.current_song_info

# --- Rotas do Painel de Administração ---

@app.route('/admin')
@auth.login_required
def admin_panel():
    status = radio.get_status()
    songs = radio.master_song_list
    jingles = radio.master_jingle_list
    ads = radio.master_ad_list
    return render_template('admin.html', status=status, songs=songs, jingles=jingles, ads=ads)

@app.route('/admin/upload', methods=['POST'])
@auth.login_required
def upload_file():
    upload_type = request.form.get('type')
    if upload_type not in ['song', 'jingle', 'ad'] or 'file' not in request.files:
        flash('Requisição inválida.', 'danger')
        return redirect(url_for('admin_panel'))

    file = request.files['file']
    if file.filename == '':
        flash('Nenhum arquivo selecionado.', 'warning')
        return redirect(url_for('admin_panel'))

    if file and file.filename.endswith('.mp3'):
        filename = secure_filename(file.filename)
        dir_map = {'song': MUSIC_DIR, 'jingle': JINGLES_DIR, 'ad': ADS_DIR}
        save_path = os.path.join(dir_map[upload_type], filename)
        file.save(save_path)
        radio.reload_master_lists(list_type=f"{upload_type}s")
        flash(f'{upload_type.capitalize()} "{filename}" enviado com sucesso!', 'success')
    else:
        flash('Formato de arquivo inválido. Apenas .mp3 é permitido.', 'danger')
        
    return redirect(url_for('admin_panel'))

@app.route('/admin/delete', methods=['POST'])
@auth.login_required
def delete_file():
    file_type = request.form.get('type')
    filename = request.form.get('filename')
    
    if not all([file_type, filename]):
        flash('Requisição de exclusão inválida.', 'danger')
        return redirect(url_for('admin_panel'))

    dir_map = {'song': MUSIC_DIR, 'jingle': JINGLES_DIR, 'ad': ADS_DIR}
    file_path = os.path.join(dir_map[file_type], filename)

    if os.path.exists(file_path):
        os.remove(file_path)
        radio.reload_master_lists(list_type=f"{file_type}s")
        flash(f'{file_type.capitalize()} "{filename}" excluído com sucesso!', 'success')
    else:
        flash('Arquivo não encontrado.', 'danger')

    return redirect(url_for('admin_panel'))

@app.route('/admin/settings/playback', methods=['POST'])
@auth.login_required
def update_playback_settings():
    mode = request.form.get('playback_mode')
    jingle_interval = request.form.get('jingle_interval')
    ad_interval = request.form.get('ad_interval')
    radio.set_playback_mode(mode)
    radio.set_intervals(jingle_interval, ad_interval)
    flash('Configurações de reprodução atualizadas!', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/settings/general', methods=['POST'])
@auth.login_required
def update_general_settings():
    new_name = request.form.get('radio_name')
    if new_name:
        radio.set_radio_name(new_name)
        flash('Nome da rádio atualizado com sucesso!', 'success')
    else:
        flash('O nome da rádio não pode ser vazio.', 'danger')
    return redirect(url_for('admin_panel'))

@app.route('/admin/playback', methods=['POST'])
@auth.login_required
def control_playback():
    action = request.form.get('action')
    if action == 'stop':
        radio.stop_playback()
        flash('Transmissão parada! Os ouvintes estão recebendo silêncio.', 'warning')
    elif action == 'start':
        radio.start_playback()
        flash('Transmissão iniciada!', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/reorder', methods=['POST'])
@auth.login_required
def reorder_files():
    data = request.get_json()
    file_type = data.get('type')
    ordered_filenames = data.get('order')

    if file_type not in ['songs', 'jingles', 'ads'] or not isinstance(ordered_filenames, list):
        return jsonify({"status": "error", "message": "Invalid data"}), 400

    radio.save_order(file_type, ordered_filenames)
    
    return jsonify({"status": "success"})

@app.route('/admin/status')
@auth.login_required
def admin_status():
    """Fornece o status completo da rádio como JSON para o frontend."""
    return jsonify(radio.get_status())

# --- Inicialização ---
if __name__ == '__main__':
    radio.start()
    host = "0.0.0.0"
    port = 8000    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--host", "-h"):
            if i+1 < len(args):
                host = args[i+1]
                i += 2
            else:
                raise SystemExit("Erro: --host precisa de um valor")
        elif a in ("--port", "-p"):
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
    print(f"Iniciando servidor de produção Waitress em http://{host}:{port}")
    print(f"Acesse o painel em http://{host}:{port}/admin (usuário: admin, senha: senha123)")
    serve(app, host=host, port=port, threads=100)