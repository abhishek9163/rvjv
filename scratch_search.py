
with open('templates/deployment.html', 'r', encoding='utf-8', errors='ignore') as f:
    for line_no, line in enumerate(f, 1):
        l = line.lower()
        if 'vehicle' in l and ('select' in l or 'input' in l or 'option' in l or 'dropdown' in l):
            print(f'{line_no}: {line.strip()[:140]}')
