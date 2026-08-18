import time
import correctmanage

samples = [
    '我爱吃苹',
    '今天的天氣真好',
    '这是一个測試，看看效果。',
    '好好学习，天天向上',
    '这是英文ABC混排测试',
    '在这种情形下，字符串里出现了123数字和ABC混合',
    '这是一个非常长的句子，用来测试性能。' * 5,
]

N = 50
start = time.time()
for i in range(N):
    for s in samples:
        correctmanage.proofread_page(s)

dt = time.time() - start
print(f'Ran {N} iterations over {len(samples)} samples in {dt:.3f}s; avg per call: {dt/(N*len(samples)):.5f}s')
