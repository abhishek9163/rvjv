import os
import glob

def fix_tyre():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base_dir, 'fleet', 'templates', 'fleet', 'tyre_pdf.html')
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            c = f.read()
        c = c.replace('🚗 Puncture', 'Puncture').replace('📌 ', '').replace('🆕 ', '').replace('👤 ', '')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(c)
        print("Tyre PDF export template fixed!")

    for w in glob.glob('/var/www/*_wsgi.py'):
        try:
            os.utime(w, None)
            print("Reloaded WSGI:", w)
        except Exception as e:
            print("WSGI reload error:", e)

if __name__ == '__main__':
    fix_tyre()
