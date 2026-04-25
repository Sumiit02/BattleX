import requests

s = requests.Session()
# login as demo player so /freefire is accessible
login = s.post('http://127.0.0.1:5000/login', data={'username':'demoplayer','password':'DEMO1234'})
r = s.get('http://127.0.0.1:5000/freefire')
text = r.text
print('status', r.status_code)
# Find grid div
start = text.find('tournament-card')
if start!=-1:
    snip_start = max(0, start-120)
    print('\n--- tournament card snippet ---')
    print(text[snip_start:snip_start+360])
else:
    print('tournament-card not found')

# Count occurrences of first event title
count_title = text.count('Lenovo Play 2 Slay - Chennai')
print('\nOccurrences of event title "Lenovo Play 2 Slay - Chennai":', count_title)
print('Contains xl:grid-cols-12:', 'xl:grid-cols-12' in text)
print('Contains h-40:', 'h-40' in text)
print('Navbar extra offset in script:', 'extra = 16' in text)
# print a short navbar script snippet
ni = text.find('siteNavbar')
if ni!=-1:
    print('\n--- navbar script snippet ---')
    start2 = text.find('<script', ni)
    if start2!=-1:
        end2 = text.find('</script>', start2)
        print(text[start2:end2+9])
