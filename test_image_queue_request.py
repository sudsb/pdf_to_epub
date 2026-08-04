import os
import tempfile
import importlib

pm = importlib.import_module('pdfmanage')
llm = importlib.import_module('llamamanage')

# create temp image
tmpd = tempfile.mkdtemp()
img = os.path.join(tmpd, 'img.png')
with open(img, 'wb') as f:
    f.write(b'\x89PNG\r\n')

# create queue and preload
q = pm.ImageQueue(store_in_memory=True)
item = q.add(img, encode=True)
# item should have base64 available
b64 = item.get_base64()
assert b64 is not None

# monkeypatch the shared session post to inspect payload
# （llamamanage 现在复用 _SESSION 连接，不再直接 requests.post）
orig_post = llm._SESSION.post

def fake_post(url, json=None, headers=None, timeout=None):
    class Resp:
        def raise_for_status(self):
            return
        def json(self):
            return {'choices':[{'message':{'content':'ok'}}]}
    # assert image is attached as an OpenAI vision content block
    content = json['messages'][0]['content']
    assert isinstance(content, list)
    image_url = content[1]['image_url']['url']
    assert image_url.startswith('data:image/png;base64,')
    assert image_url.endswith(b64)
    return Resp()

llm._SESSION.post = fake_post
res = llm.request_image('please output original', b64, 'HY', False, True)
assert isinstance(res, dict)
assert res['result'] == 'ok'

# cleanup
llm._SESSION.post = orig_post
print('test passed')
