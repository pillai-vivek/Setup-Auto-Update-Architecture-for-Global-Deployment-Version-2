#!/usr/bin/env python3

import os
import sys
import json
import tempfile
import requests
from git import Repo

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
        print(f"[✓] Zabbix login successful.")
        return result['result']
    else:
        print(f"[✗] Zabbix login failed: {result}")
        sys.exit(1)

# ====== IMPORT ZABBIX TEMPLATE ======
def import_zabbix_template(auth_token, template_path):
    import os
    import requests

    ext = template_path.split('.')[-1].lower()
    format_map = {"xml": "xml", "json": "json", "yaml": "yaml", "yml": "yaml"}

    if ext not in format_map:
        print(f"[WARN] Unsupported template format: {template_path}")
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
            print(f"[Zabbix] Import failed for {os.path.basename(template_path)}: {response_json['error']['data']}")
        else:
            print(f"[Zabbix] Successfully imported {os.path.basename(template_path)}")
    except ValueError:
        print(f"[Zabbix] Invalid response format for {os.path.basename(template_path)}: {res.text}")

# --- Copy Scripts ---
def copy_external_script(src_path):
    dst_path = os.path.join(CONFIG['externalscript_path'], os.path.basename(src_path))
    os.system(f'cp {src_path} {dst_path}')
    if dst_path.endswith((".sh", ".py")):
        os.system(f'chmod +x {dst_path}')
        print(f"[Zabbix] Script copied and executable: {dst_path}")

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
    print(f"[Grafana] Upload {os.path.basename(json_path)}: {res.status_code}")

# --- Git Pull ---
def clone_or_pull(repo_url, local_dir):
    if os.path.exists(local_dir):
        Repo(local_dir).remotes.origin.pull()
    else:
        Repo.clone_from(repo_url, local_dir)
    return local_dir

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
    print("[*] Cloning GitHub repos...")
    zbx_tpl_dir = clone_or_pull(CONFIG['git_repos']['zabbix_templates'], os.path.join(temp_dir, 'zbx_tpl'))
    zbx_scr_dir = clone_or_pull(CONFIG['git_repos']['zabbix_scripts'], os.path.join(temp_dir, 'zbx_scr'))
    graf_dir = clone_or_pull(CONFIG['git_repos']['grafana_dashboards'], os.path.join(temp_dir, 'graf_dash'))

    print("[*] Zabbix login...")
    auth_token = zabbix_login()

    for cat in CATEGORIES:
        print(f"\n[→] Processing category: {cat}")
        subdir_tpl = os.path.join(zbx_tpl_dir, cat)
        subdir_scr = os.path.join(zbx_scr_dir, cat)
        subdir_graf = os.path.join(graf_dir, cat)

        print("[*] Importing Zabbix templates...")
        if os.path.exists(subdir_tpl):
            for f in os.listdir(subdir_tpl):
                if f.lower().endswith(('.xml', '.json', '.yaml', '.yml')):
                    import_zabbix_template(auth_token, os.path.join(subdir_tpl, f))

        print("[*] Copying Zabbix scripts...")
        if os.path.exists(subdir_scr):
            for f in os.listdir(subdir_scr):
                full_path = os.path.join(subdir_scr, f)
                if os.path.isfile(full_path):
                    copy_external_script(full_path)

        print("[*] Uploading Grafana dashboards...")
        if os.path.exists(subdir_graf):
            for f in os.listdir(subdir_graf):
                if f.endswith(".json"):
                    upload_grafana_dashboard(os.path.join(subdir_graf, f))

    if CONFIG.get("venv_required", False):
        print("[*] Setting up virtualenv...")
        setup_virtualenv()

    print("[✔] Auto-update finished successfully.")

if __name__ == "__main__":
    main()
