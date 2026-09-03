import os
import re
import sys
from jinja2 import TemplateNotFound, TemplateSyntaxError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(BASE_DIR, 'templates')
sys.path.insert(0, BASE_DIR)
from app import app

env = app.jinja_env
referenced_templates = set()
template_pattern = re.compile(r"(?:render_template|extends|include)\s*\(?\s*['\"]([^'\"]+)")

for filename in sorted(os.listdir(template_dir)):
    if not filename.endswith('.html'):
        continue
    try:
        env.get_template(filename)
        print('OK:', filename)
    except (TemplateNotFound, TemplateSyntaxError) as error:
        print('ERROR in', filename, error)
        sys.exit(1)

    with open(os.path.join(template_dir, filename), encoding='utf-8') as template_file:
        referenced_templates.update(template_pattern.findall(template_file.read()))

missing = sorted(name for name in referenced_templates if not os.path.exists(os.path.join(template_dir, name)))
if missing:
    print('Missing templates:', ', '.join(missing))
    sys.exit(1)

print('All templates are present and syntactically valid.')
