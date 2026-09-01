import urllib.request, json
url = 'https://api.github.com/repos/ggml-org/llama.cpp/issues?per_page=100'
data = json.load(urllib.request.urlopen(url))
for issue in data:
    title = issue.get('title', '').lower()
    if any(kw in title for kw in ['500', 'internal server error', 'glm-ocr', 'chat_template_kwargs', 'enable_thinking', 'multimodal', 'vision']):
        print(f"#{issue['number']}: {issue['title']} - {issue['html_url']}")