from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError
import sys
env = Environment(loader=FileSystemLoader(r'C:\gaming site\templates'))
files = ['freefire.html','register_freefire.html']
ok=True
for f in files:
    try:
        env.get_template(f)
        print('OK:',f)
    except TemplateSyntaxError as e:
        ok=False
        print('ERROR in',f, e)
sys.exit(0 if ok else 1)
