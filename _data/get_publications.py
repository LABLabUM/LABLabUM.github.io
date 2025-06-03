import json
from bs4 import BeautifulSoup

# 假设你的HTML文件名为 'haoyunzhang_scholar.html'
with open(r"C:\Users\User\Desktop\Lab_official_web\LABLabUM.github.io\_data\_Haoyun Zhang_ - _Google 学术搜索_.html", 'r', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'html.parser')
publications = []

# 查找所有出版物条目
for entry in soup.select('.gsc_a_tr'):
    title_tag = entry.select_one('.gsc_a_at')
    title = title_tag.text if title_tag else ''
    link = 'https://scholar.google.com' + title_tag['href'] if title_tag and title_tag.has_attr('href') else ''
    # 获取作者和期刊信息
    gray_divs = entry.select('.gsc_a_t .gs_gray')
    authors = gray_divs[0].text if len(gray_divs) > 0 else ''
    journal = gray_divs[1].text if len(gray_divs) > 1 else ''
    year_tag = entry.select_one('.gsc_a_y span')
    year = year_tag.text if year_tag else ''
    publications.append({
        'title': title,
        'link': link,
        'authors': authors,
        'journal': journal,
        'year': year
    })

# 保存为JSON文件
with open('_data\publications.json', 'w', encoding='utf-8') as f:
    json.dump(publications, f, ensure_ascii=False, indent=2)