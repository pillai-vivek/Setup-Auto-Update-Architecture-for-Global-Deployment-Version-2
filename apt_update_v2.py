#!/usr/bin/env python3

import os
import sys
import json
import tempfile
import requests
import zipfile
import logging
from git import Repo
from datetime import datetime
from logging.handlers import RotatingFileHandler

# --- Logging Setup ---
LOG_DIR = "/var/log/zabbix-auto-update"
LOG_FILE = os.path.join(LOG_DIR, "zabbix_auto_update.log")
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5MB
BACKUP_COUNT = 3

class ZippingRotatingFileHandler(RotatingFileHandler):
    def doRollover(self):
        super().doRollover()
        log_filename = f"{self.baseFilename}.1"
        if os.path.exists(log_filename):
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            zip_filename = f"{log_filename}_{timestamp}.zip"
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(log_filename, os.path.basename(log_filename))
            os.remove(log_filename)

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = ZippingRotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT)
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

# Load config
config_path = sys.argv[1] if len(sys.argv) > 1 else 'auto_update_config_v2.json'
with open(config_path) as f:
    CONFIG = json.load(f)

CATEGORIES = [cat.strip() for cat in CONFIG.get("category", "").split(",") if cat.strip()]

# --- Zabbix Login ---
def zabbix_login():
    payload = {
        "jsonrpc": "2.0",
        "method": "user.login",
        "params": {
            "username": CONFIG['zabbix']['user'],
            "password": CONFIG['zabbix']['password']
        },
        "id": 1
    }
    res = requests.post(CONFIG['zabbix']['url'], json=payload, headers={"Content-Type": "application/json"})
    result = res.json()
    if 'result' in result:
        logger.info("Zabbix login successful.")
        return result['result']
    else:
        logger.error(f"Zabbix login failed: {result}")
        sys.exit(1)

# ====== IMPORT ZABBIX TEMPLATE ======
def import_zabbix_template(auth_token, template_path):
    ext = template_path.split('.')[-1].lower()
    format_map = {"xml": "xml", "json": "json", "yaml": "yaml", "yml": "yaml"}

    if ext not in format_map:
        logger.warning(f"Unsupported template format: {template_path}")
        return

    with open(template_path, 'r', encoding='utf-8') as file:
        source = file.read()

    payload = {
        "jsonrpc": "2.0",
        "method": "configuration.import",
        "params": {
            "format": format_map[ext],
            "rules": {
                "templates": {"createMissing": True, "updateExisting": True},
                "items": {"createMissing": True, "updateExisting": True},
                "triggers": {"createMissing": True, "updateExisting": True},
                "discoveryRules": {"createMissing": True, "updateExisting": True},
                "graphs": {"createMissing": True, "updateExisting": True},
                "valueMaps": {"createMissing": True, "updateExisting": True},
                "httptests": {"createMissing": True, "updateExisting": True}
            },
            "source": source
        },
        "id": 2
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}"
    }

    res = requests.post(CONFIG['zabbix']['url'], json=payload, headers=headers)
    try:
        response_json = res.json()
        if "error" in response_json:
            logger.error(f"Import failed for {os.path.basename(template_path)}: {response_json['error']['data']}")
        else:
            logger.info(f"Successfully imported {os.path.basename(template_path)}")
    except ValueError:
        logger.error(f"Invalid response for {os.path.basename(template_path)}: {res.text}")

# --- Copy Scripts ---
def copy_external_script(src_path):
    dst_path = os.path.join(CONFIG['externalscript_path'], os.path.basename(src_path))
    os.system(f'cp {src_path} {dst_path}')
    if dst_path.endswith((".sh", ".py")):
        os.system(f'chmod +x {dst_path}')
        logger.info(f"Script copied and made executable: {dst_path}")

# --- Upload Grafana ---
def upload_grafana_dashboard(json_path):
    with open(json_path, 'r') as file:
        dashboard_json = json.load(file)
    payload = {"dashboard": dashboard_json, "overwrite": True}
    headers = {
        "Authorization": f"Bearer {CONFIG['grafana']['api_key']}",
        "Content-Type": "application/json"
    }
    res = requests.post(f"{CONFIG['grafana']['url']}/api/dashboards/db", headers=headers, json=payload)
    logger.info(f"Upload {os.path.basename(json_path)}: {res.status_code}")

# --- Git Pull ---
def clone_or_pull(repo_url, local_dir):
    if os.path.exists(local_dir):
        Repo(local_dir).remotes.origin.pull()
    else:
        Repo.clone_from(repo_url, local_dir)
    return local_dir

#---Install Grafana Plugin---
def install_grafana_plugins(plugin_file):
    if not os.path.exists(plugin_file):
        logger.warning(f"Grafana plugin file not found: {plugin_file}")
        return

    with open(plugin_file, "r") as f:
        plugins = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    # Track whether any new plugin was installed
    plugin_installed = False

    for plugin in plugins:
        logger.info(f"Installing Grafana plugin: {plugin}")
        exit_code = os.system(f"grafana-cli plugins install {plugin}")
        if exit_code == 0:
            logger.info(f"Successfully installed: {plugin}")
            plugin_installed = True
        else:
            logger.warning(f"Plugin might already be installed or failed: {plugin}")

    # Restart Grafana only if something new was installed
    if plugin_installed:
        os.system("systemctl restart grafana-server")
        logger.info("Grafana server restarted after plugin installation.")

    # Enable Zabbix data source (if not already added)
    try:
        add_zabbix_datasource()
    except Exception as e:
        logger.error(f"Failed to add Zabbix data source: {e}")


#---Adding Zabbix DataSource---
def add_zabbix_datasource():
    logger.info("[*] Ensuring Zabbix data source is configured in Grafana...")
    headers = {
        "Authorization": f"Bearer {CONFIG['grafana']['api_key']}",
        "Content-Type": "application/json"
    }

    # Check if Zabbix data source already exists
    res = requests.get(f"{CONFIG['grafana']['url']}/api/datasources", headers=headers)
    if res.status_code != 200:
        logger.warning("[Grafana] Failed to list data sources")
        return

    for ds in res.json():
        if ds.get("type") == "alexanderzobnin-zabbix-datasource":
            logger.info("[Grafana] Zabbix data source already exists.")
            return

    # If not found, add Zabbix data source
    payload = {
        "name": "Zabbix",
        "type": "alexanderzobnin-zabbix-datasource",
        "access": "proxy",
        "url": CONFIG['zabbix']['url'].replace("/api_jsonrpc.php", ""),
        "basicAuth": False,
        "jsonData": {
            "username": CONFIG['zabbix']['user'],
            "password": CONFIG['zabbix']['password'],
            "dbConnectionEnable": False,
            "trends": True,
            "trendsFrom": "7d",
            "trendsRange": "4d"
        },
        "secureJsonData": {
            "password": CONFIG['zabbix']['password']
        }
    }

    res = requests.post(f"{CONFIG['grafana']['url']}/api/datasources", headers=headers, json=payload)
    if res.status_code == 200:
        logger.info("[Grafana] Zabbix data source successfully added.")
    else:
        logger.warning(f"[Grafana] Failed to add Zabbix data source: {res.text}")



# def install_grafana_plugins(plugin_file):
#     if not os.path.exists(plugin_file):
#         logger.warning(f"Grafana plugin file not found: {plugin_file}")
#         return

#     with open(plugin_file, "r") as f:
#         plugins = [line.strip() for line in f if line.strip() and not line.startswith("#")]

#     for plugin in plugins:
#         logger.info(f"Installing Grafana plugin: {plugin}")
#         exit_code = os.system(f"grafana-cli plugins install {plugin}")
#         if exit_code == 0:
#             logger.info(f"Successfully installed: {plugin}")
#         else:
#             logger.error(f"Failed to install: {plugin}")

# --- Setup venv ---
def setup_virtualenv():
    import subprocess
    venv_dir = os.path.join(CONFIG['externalscript_path'], 'venv')
    requirements_file = os.path.join(CONFIG['externalscript_path'], 'requirements.txt')
    if not os.path.exists(venv_dir):
        subprocess.run(["python3", "-m", "venv", venv_dir], check=True)
    pip_path = os.path.join(venv_dir, "bin", "pip")
    subprocess.run([pip_path, "install", "--upgrade", "pip"], check=True)
    if os.path.exists(requirements_file):
        subprocess.run([pip_path, "install", "-r", requirements_file], check=True)

# --- Main ---
def main():
    temp_dir = tempfile.mkdtemp()
    logger.info("[*] Cloning GitHub repos...")
    zbx_tpl_dir = clone_or_pull(CONFIG['git_repos']['zabbix_templates'], os.path.join(temp_dir, 'zbx_tpl'))
    zbx_scr_dir = clone_or_pull(CONFIG['git_repos']['zabbix_scripts'], os.path.join(temp_dir, 'zbx_scr'))
    graf_dir = clone_or_pull(CONFIG['git_repos']['grafana_dashboards'], os.path.join(temp_dir, 'graf_dash'))

    logger.info("[*] Zabbix login...")
    auth_token = zabbix_login()

    for cat in CATEGORIES:
        logger.info(f"\n[→] Processing category: {cat}")
        subdir_tpl = os.path.join(zbx_tpl_dir, cat)
        subdir_scr = os.path.join(zbx_scr_dir, cat)
        subdir_graf = os.path.join(graf_dir, cat)

        logger.info("[*] Importing Zabbix templates...")
        if os.path.exists(subdir_tpl):
            for f in os.listdir(subdir_tpl):
                if f.lower().endswith(('.xml', '.json', '.yaml', '.yml')):
                    import_zabbix_template(auth_token, os.path.join(subdir_tpl, f))

        logger.info("[*] Copying Zabbix scripts...")
        if os.path.exists(subdir_scr):
            for f in os.listdir(subdir_scr):
                full_path = os.path.join(subdir_scr, f)
                if os.path.isfile(full_path):
                    copy_external_script(full_path)

        logger.info("[*] Uploading Grafana dashboards...")
        if os.path.exists(subdir_graf):
            for f in os.listdir(subdir_graf):
                if f.endswith(".json"):
                    upload_grafana_dashboard(os.path.join(subdir_graf, f))

    if CONFIG.get("venv_required", False):
        logger.info("[*] Setting up virtualenv...")
        setup_virtualenv()

    # Install Grafana plugins if grafana_plugins.txt exists
    plugin_file_path = os.path.join(graf_dir, "grafana_plugins.txt")
    install_grafana_plugins(plugin_file_path)

    # Ensure Zabbix data source is added
    add_zabbix_datasource()

    logger.info("[✔] Auto-update finished successfully.")

# def main():
#     global logger
#     logger = setup_logging()
#     logger.info("Starting Zabbix auto-update process...")

#     temp_dir = tempfile.mkdtemp()
#     logger.info("Cloning GitHub repos...")
#     zbx_tpl_dir = clone_or_pull(CONFIG['git_repos']['zabbix_templates'], os.path.join(temp_dir, 'zbx_tpl'))
#     zbx_scr_dir = clone_or_pull(CONFIG['git_repos']['zabbix_scripts'], os.path.join(temp_dir, 'zbx_scr'))
#     graf_dir = clone_or_pull(CONFIG['git_repos']['grafana_dashboards'], os.path.join(temp_dir, 'graf_dash'))

#     logger.info("Logging in to Zabbix...")
#     auth_token = zabbix_login()

#     for cat in CATEGORIES:
#         logger.info(f"Processing category: {cat}")
#         subdir_tpl = os.path.join(zbx_tpl_dir, cat)
#         subdir_scr = os.path.join(zbx_scr_dir, cat)
#         subdir_graf = os.path.join(graf_dir, cat)

#         if os.path.exists(subdir_tpl):
#             for f in os.listdir(subdir_tpl):
#                 if f.lower().endswith(('.xml', '.json', '.yaml', '.yml')):
#                     import_zabbix_template(auth_token, os.path.join(subdir_tpl, f))

#         if os.path.exists(subdir_scr):
#             for f in os.listdir(subdir_scr):
#                 full_path = os.path.join(subdir_scr, f)
#                 if os.path.isfile(full_path):
#                     copy_external_script(full_path)

#         if os.path.exists(subdir_graf):
#             for f in os.listdir(subdir_graf):
#                 if f.endswith(".json"):
#                     upload_grafana_dashboard(os.path.join(subdir_graf, f))

#     # Install Grafana plugins from plugin file
#     plugin_file_path = os.path.join(graf_dir, "grafana_plugins.txt")
#     install_grafana_plugins(plugin_file_path)

#     # Optional: Restart Grafana
#     logger.info("Restarting Grafana server to apply plugin changes...")
#     os.system("systemctl restart grafana-server")

#     if CONFIG.get("venv_required", False):
#         logger.info("Setting up virtual environment...")
#         setup_virtualenv()

#     logger.info("Auto-update finished successfully.")


if __name__ == "__main__":
    main()

