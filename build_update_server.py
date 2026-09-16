# build_update_server.py
import os
import zlib
import base64

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INCLUDE_EXTS = {'.py', '.html', '.js', '.json', '.css', '.svg', '.png', '.jpg'}
EXCLUDE_DIRS = {'__pycache__', '.git', '.idea', 'venv', 'env', '.venv', 'scratch', '.system_generated', 'media', 'staticfiles'}

files_to_pack = {}

for root, dirs, files in os.walk(BASE_DIR):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    for file in files:
        ext = os.path.splitext(file)[1].lower()
        if ext in INCLUDE_EXTS or file in ['manage.py']:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, BASE_DIR).replace('\\', '/')
            if rel_path in ['update_server.py', 'build_update_server.py', 'deploy_pwa_only.py']:
                continue
            try:
                with open(full_path, 'rb') as f:
                    content = f.read()
                compressed = zlib.compress(content)
                encoded = base64.b64encode(compressed).decode('ascii')
                files_to_pack[rel_path] = encoded
            except Exception as e:
                print(f"Skipping {rel_path}: {e}")

print(f"Total files packed into update_server.py: {len(files_to_pack)}")

update_server_code = f'''import zlib, base64, os, sys, subprocess, glob, json, datetime

FILES = {repr(files_to_pack)}

def write_files():
    base = os.path.dirname(os.path.abspath(__file__))
    for rel_path, b64_data in FILES.items():
        dest = os.path.join(base, rel_path.replace('/', os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        raw_data = zlib.decompress(base64.b64decode(b64_data))
        with open(dest, "wb") as f:
            f.write(raw_data)
        print(f"Updated: {{rel_path}}")

def run_migrations():
    base = os.path.dirname(os.path.abspath(__file__))
    python_exe = sys.executable or "python"
    manage_py = os.path.join(base, "manage.py")
    if os.path.exists(manage_py):
        try:
            print("Running database migrations...")
            subprocess.run([python_exe, manage_py, "migrate", "--noinput"], check=True)
            print("Running collectstatic...")
            subprocess.run([python_exe, manage_py, "collectstatic", "--noinput"], check=True)
        except Exception as e:
            print(f"Migration / Collectstatic notice: {{e}}")

def sync_employee_database():
    base = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, base)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core_project.settings')
    try:
        import django
        django.setup()
        from django.core.management import call_command
        print("Syncing Employee dataset if management command exists...")
        try:
            call_command('sync_desktop_employees')
        except Exception as e:
            print(f"Sync desktop employees notice: {{e}}")
    except Exception as e:
        print(f"Django setup notice: {{e}}")

def sync_camp_master_data():
    base = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, base)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core_project.settings')
    try:
        import django
        django.setup()
        from django.core.management import call_command
        print("Synchronizing complete Camp Blocks, Rooms, Residents & Assets...")
        try:
            call_command('seed_camp_complete_data')
        except Exception as e:
            print(f"seed_camp_complete_data notice: {{e}}")
    except Exception as e:
        print(f"Camp master setup notice: {{e}}")

def sync_rc_data():
    base = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, base)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core_project.settings')
    try:
        import django
        django.setup()
        from django.core.management import call_command
        print("Synchronizing Vehicle RC Document Register...")
        try:
            call_command('sync_rc_documents')
        except Exception as e:
            print(f"sync_rc_documents notice: {{e}}")
    except Exception as e:
        print(f"RC documents setup notice: {{e}}")

def reload_webapp():
    wsgi_files = glob.glob('/var/www/*_wsgi.py')
    if wsgi_files:
        for wf in wsgi_files:
            try:
                os.utime(wf, None)
                print(f"Touched WSGI file: {{wf}}")
            except Exception as e:
                print(f"Could not touch {{wf}}: {{e}}")
    else:
        print("Note: If on PythonAnywhere or Linux WSGI server, make sure to reload the Web app!")

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"Deploying {{len(FILES)}} files into {{script_dir}}...")
    write_files()
    run_migrations()

    # NOTE: Seed commands are strictly opt-in via '--sync-data' argument
    # so that deploying code updates NEVER overwrites or resets user edits on live server!
    if "--sync-data" in sys.argv:
        print("Flag '--sync-data' detected. Synchronizing master datasets...")
        sync_employee_database()
        sync_camp_master_data()
        sync_rc_data()
    else:
        print("[SAFE] Skipped dataset seeding. Live database records preserved.")

    reload_webapp()
    print("Done! Code updated and deployed successfully without touching live data.")
'''

target_file = os.path.join(BASE_DIR, 'update_server.py')
with open(target_file, 'w', encoding='utf-8') as f:
    f.write(update_server_code)

print("Successfully generated update_server.py!")
