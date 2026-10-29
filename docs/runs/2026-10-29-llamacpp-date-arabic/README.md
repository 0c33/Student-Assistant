# Run · 2026-10-29 · an Arabic date question, answered from context

Evidence for Phase 2 (context engineering). Synthetic vault (`vault/`). The question needs today's date (now in
the system prompt), the exam date (Data.md, injected) and the pending work (MEMORY.MD, injected).

| | |
|---|---|
| Engine / model | llama.cpp server on the author's LAN (`SA_PROVIDER=custom`), `qwen3.8-flash-next-iq3s` |
| Command | `uv run main.py ask --session live "كم باقي على اختبار الذكاء الاصطناعي النهائي؟"` ("How long until the AI final?") |
| Result | `stopped: done`, **1 model call, no tool call needed**, 2,752 prompt + 161 completion tokens |

## The answer
```text
باقي **22 يوم** على اختبار الذكاء الاصطناعي النهائي 🗓️
- التاريخ: الجمعة 20 نوفمبر 2026 (اليوم الخميس 29 أكتوبر)
- عندك بعدُه اختبار Cloud Computing النهائي في 3 ديسمبر (باقي 35 يوم)
ملاحظة: لسه ما بدأنا المراجعة — في قائمة أعمالك إنك تراجع **الفصل الثالث** قبل اختبار AI. تبغى أجهزلك خطة مذاكرة مقسمة على الـ22 يوم؟
```
Correct: 29 Oct → 20 Nov is 22 days, → 3 Dec is 35 days. Replied in Arabic; used MEMORY.MD's pending item.

## Context anatomy (`uv run main.py context`, estimated at 3 characters per token)
| Part | Before the task | Next request |
|---|---:|---:|
| system · spec (`spec/AGENTS.md`) | 1,793 | 1,793 |
| system · Data.md | 63 | 63 |
| system · date | 20 | 20 |
| system · MEMORY.MD | 70 | 70 |
| system · summaries | – | 143 |
| tool definitions | 1,343 | 1,343 |
| history (user + assistant) | 0 | 146 |
| **total** | **3,289** | **3,578** |

The provider counted 2,752 prompt tokens for the first request: the estimate is ~1.2× pessimistic, the safe side
for a budget. The tool definitions are ~40% of an empty request: the next thing to trim if the window gets tight.
