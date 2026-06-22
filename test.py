from openai import OpenAI

client = OpenAI(
    base_url="https://hotaruapi.com/v1",
    api_key="sk-3x2cUvPm8ZusO1MqLMOpcOivDZ5vjeKZ06cEHwNVlIrP7fVf" # 在 /console/token 生成
)

completion = client.chat.completions.create(
    model="gpt-5.5",
    messages=[{"role": "user", "content": "你好，力挽狂澜的萤火虫！"}]
)
print(completion.choices[0].message.content)