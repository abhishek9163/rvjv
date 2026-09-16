import os
import glob

def fix_server():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    files_to_fix = [
        os.path.join(base_dir, 'templates', 'deployment.html'),
        os.path.join(base_dir, 'templates', 'allocation_print.html'),
        os.path.join(base_dir, 'templates', 'deployment_export.html'),
    ]
    
    for p in files_to_fix:
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                content = f.read()
            
            old_emp = '{{ a.driver_emp_id|default:a.driver.emp_id|default:"-" }}'
            new_emp = '{% if a.driver_emp_id %}{{ a.driver_emp_id }}{% elif a.driver and a.driver.emp_id %}{{ a.driver.emp_id }}{% else %}-{% endif %}'
            
            old_con = '{{ a.driver_contact|default:a.driver.contact_info|default:"-" }}'
            new_con = '{% if a.driver_contact %}{{ a.driver_contact }}{% elif a.driver and a.driver.contact_info %}{{ a.driver.contact_info }}{% else %}-{% endif %}'
            
            content = content.replace(old_emp, new_emp)
            content = content.replace(old_con, new_con)
            
            with open(p, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"Fixed template: {p}")
    
    # Reload PythonAnywhere WSGI
    wsgi_files = glob.glob('/var/www/*_wsgi.py')
    if wsgi_files:
        for wf in wsgi_files:
            try:
                os.utime(wf, None)
                print(f"Reloaded PythonAnywhere WSGI webapp: {wf}")
            except Exception as e:
                print(f"Could not touch {wf}: {e}")
    else:
        print("Note: Please click 'Reload' in PythonAnywhere Web Tab to apply changes.")

if __name__ == '__main__':
    fix_server()
