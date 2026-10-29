import requests, json, os
from dotenv import load_dotenv
from datetime import datetime
from typing import Dict, Any

os.sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))) if hasattr(os, 'sys') else None

load_dotenv()


class Agent:

    def __init__(self):
        self.Data = {}
        self.path = os.path.expanduser(os.getenv('SA_ROOT', 'vault'))
        self.chat_history_list = []
        self.session_count = 0
        self.max_history_entries = 50
        self.recent_history_limit = 5  # Sliding window for prompt context

        self.Tools = {
            "write_file": self.write_file,
            'read_file': self.read_file,
            'list_dir': self.list_dir,
            'user_input': self.user_input,
            'replace_in_file': self.replace_in_file,
            'remove_from_file': self.remove_from_file,
            # 'sync_obsidian_graph': self.sync_obsidian_graph,
            # 'graph_status': self.graph_status,
            'make_dir': self.make_dir
        }

        while True:
            self.Main_Agent()



    def LLM(self, content: list) -> Dict[str, Any]:
        try:
            res = requests.post(os.getenv('SA_BASE_URL', 'http://localhost:8080/v1') + '/chat/completions', json={"messages": content, "tools": self.get_tools(), "reasoning_max_tokens": 256})
                # for line in r.iter_lines(decode_unicode=True):
                #     if not line:
                #         continue
                #     if line.startswith("data: "):
                #         data = line[len("data: "):]
                #         if data == "[DONE]":
                #             break
                #         chunk = json.loads(data)
            res.raise_for_status()
            return res.json()['choices'][0]['message']
        except Exception as e:
            return f'LLM Error: {e}'

    def write_file(self, file_name, content, write_mode='a'):
        try:
            with open(file_name, write_mode) as f:
                f.write(content)
            return f'Done writing to file: {file_name},\n with content: [{content}]'
        except Exception as e:
            return f'Error in writing file: {file_name}, error logs: {e}'

    def read_file(self, file_name):
        try:
            with open(file_name, 'r', encoding='utf-8') as f:
                return f'Reading file {file_name} content: {f.read()}'
        except Exception as e:
            return f'Error in reading file: {file_name}, error logs: {e}'

    def replace_in_file(self, file_name, old_content, new_content):
        try:
            with open(file_name, 'r') as f:
                content = f.read()
            if old_content not in content:
                return f'Error: "{old_content}" not found in {file_name}'
            new_file_content = content.replace(old_content, new_content)
            with open(file_name, 'w') as f:
                f.write(new_file_content)
            return f'Successfully replaced "{old_content}" with "{new_content}" in {file_name}'
        except Exception as e:
            return f'Error in replacing in file: {file_name}, error logs: {e}'

    def remove_from_file(self, file_name, content_to_remove):
        try:
            with open(file_name, 'r') as f:
                content = f.read()
            if content_to_remove not in content:
                return f'Error: "{content_to_remove}" not found in {file_name}'
            new_file_content = content.replace(content_to_remove, '')
            with open(file_name, 'w') as f:
                f.write(new_file_content)
            return f'Successfully removed "{content_to_remove}" from {file_name}'
        except Exception as e:
            return f'Error in removing from file: {file_name}, error logs: {e}'

    def make_dir(self, directory_path):
      try:
          os.makedirs(directory_path, exist_ok=True)
          return f'✅ Directory created/verified: {directory_path}'
      except Exception as e:
          return f'❌ Error creating directory: {directory_path}, error: {e}'

    def list_dir(self, directory):
        return os.listdir(directory)

    def user_input(self, question):
        print(question, '\n\n')
        user_input = input('User Input: ')
        return user_input

    def tool_execution(self, tool_name, **args):
        return self.Tools[tool_name](**args)

    def _llm_json_fallback(self, broken_data, errors):
        result = self.LLM(f"""
Fix this broken JSON. Return ONLY valid RAW JSON, no backticks, no explanations.
Errors encountered: {errors}
Broken JSON:
{broken_data}""")
        return result.strip()

    def handle_json_error(self, data):
        from json_utils import JsonFixer
        parsed, success, msg = JsonFixer.validate_and_fix(data, self._llm_json_fallback)
        if not success:
            raise json.JSONDecodeError(f'All JSON fix strategies failed: {msg}', data, 0)
        return parsed

    def summarize_agent(self, to_be_summarized):

        data = []

        data.append({"role": "system", "content": f"""
                you are summarize chat history agent.
                you will only return the summarized content, no explanations ever !!.
                you will return RAW JSON format no backticks.
    
                summarize RULES:
                    - DON'T EVER LOSE THE MEANING.
                    - DON't DUPLICATE INFORMATION.
                    - IF THERE DUPLICATE TEXT, YOU ONLY MENTION IT ONCE.
                    - YOU HAVE TO RETURN ONLY WHAT HAPPENS IN THIS CHAT HISTORY AND ANALZYE IT AND THEN SUMMARIZE IT INTO ONE MESSAGE.
                    - YOU WILL RETURN WHAT HAVE IT DONE IN THIS SESSION AND WHAT THE PROGRESS AND WHAT ITS ABOUT.
    
                OUTPUT Template:
                    {{
                        "user_input": "here you will summarize all the user requests/inputs",
                        "Agent_Response": "here you will return the agent summarized Responses"}}"""})

        data.append({"role": "user", "content": f"CHAT HISTORY: {json.dumps(to_be_summarized, ensure_ascii=False)}"})

        result = self.LLM(data)

        print(f'\n\nThis is Data to be summarized: {data}\n\n\n\nData Summerized: {result}\n\n\n\n')

        return result

    def security_agent(self, command):
        result = self.LLM('you are a security command approval or rejecting,')

    def get_tools(self):

        return [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read the contents of a file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "file_name": {"type": "string", "description": "The path/name of the file to read"}
                            },
                            "required": ["file_name"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "description": "Write or append content to a file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "file_name": {"type": "string", "description": "The path/name of the file"},
                                "content": {"type": "string", "description": "The content to write"},
                                "write_mode": {"type": "string", "enum": ["a", "w"], "description": "Append (a) or overwrite (w)"}
                            },
                            "required": ["file_name", "content"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "replace_in_file",
                        "description": "Replace specific content inside a file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "file_name": {"type": "string"},
                                "old_content": {"type": "string", "description": "Exact text to find"},
                                "new_content": {"type": "string", "description": "Text to replace it with"}
                            },
                            "required": ["file_name", "old_content", "new_content"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "remove_from_file",
                        "description": "Remove specific content from a file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "file_name": {"type": "string"},
                                "content_to_remove": {"type": "string"}
                            },
                            "required": ["file_name", "content_to_remove"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "list_dir",
                        "description": "List files in a directory",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "directory": {"type": "string", "description": "The directory path"}
                            },
                            "required": ["directory"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "make_dir",
                        "description": "Create a new directory",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "directory_path": {"type": "string"}
                            },
                            "required": ["directory_path"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "user_input",
                        "description": "Ask the user a question and wait for their response",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string", "description": "The question to ask the user"}
                            },
                            "required": ["question"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "sync_obsidian_graph",
                        "description": "Validate and fix frontmatter/wikilinks in Obsidian",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "directory": {"type": "string", "description": "Path to sync"}
                            },
                            "required": ["directory"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "graph_status",
                        "description": "Returns current graph link status",
                        "parameters": {"type": "object", "properties": {}}
                    }
                }
            ]

    def Main_Agent(self, *data):

        session_history = []
        chat_turn_count = 0


        session_history.append({
                "role": "system", "content": f"""
                    # IDENTITY

                    You are the Main Agent — a persistent, autonomous student helper AI for Sami.
                    You are his second brain. You organize his work, track his projects, his exams, log his progress,
                    manage his files, and ensure he never loses track of what he has, what he's working on,
                    and what comes next.

                    You operate in a dual-loop system:
                    - Inner loop: you drive a single task to completion using tools and reasoning.
                    - Outer loop: you persist between tasks, stay alive, and wait for the next one.

                    When a task completes, the conversation is summarized and injected into your context
                    for the next task. You don't carry the full raw transcript forward.

                    You are proactive, structured, and concise. You suggest, plan, and execute.
                    You don't wait to be told everything.

                    You can ask the user until you have understand his request, and you must understand it well!.

                    # WHO I'M SERVICING

                    ## DATA.MD (it will have all the information about my college data)
                    {self.read_file('Data.md')}

                    # YOUR OWN CODE

                    Your source code lives at main.py. You can read it at any time using read_file
                    to understand how you are implemented, how your loop works, how tools are
                    executed, and what logic drives your behavior.

                    - Read it when you want to propose or make an upgrade to your own implementation.
                    - Read it when something unexpected happens and you need to debug.
                    - Read it when Sami asks how a specific feature works under the hood.
                    - Do NOT read it every turn. Only when relevant.

                    If you identify an improvement or a fix, explain it to Sami in chat first.
                    Only modify the code if he explicitly approves.

                    # TOOL PROTOCOL

                    You have read and write access to the file system. Use the provided tools for all
                    file operations. Never simulate file operations in your text response.

                    ## Rules

                    1. Always read_file before replace_in_file or remove_from_file.
                    2. When creating a new project, make_dir first, then write_file inside it.
                    3. Keep file content clean, structured, and well-organized.
                    4. If a file doesn't exist and you need it, create it with a sensible header.
                    5. Batch related tool calls when possible.
                    6. Use user_input only when you genuinely need Sami's decision.
                    Do not use it for confirming things you can decide yourself.
                    7. After structural changes to Obsidian files, run sync_obsidian_graph.

                    # MEMORY PROTOCOL

                    MEMORY.MD is your long-term memory. Treat it as a living document.

                    ## Update MEMORY.MD when

                    - Sami makes a new decision or changes direction.
                    - A project starts, changes status, or completes.
                    - You learn new important context about Sami or his work.
                    - You make a significant structural change to files.
                    - You finish a multi-step task.

                    ## Structure

                        ## Active Context
                        - [Current project/task state]

                        ## Recent Decisions
                        - [Date] [Decision and why]

                        ## Pending / Follow-up
                        - [Things to check or do next]

                        ## Lessons & Patterns
                        - [What works, what doesn't]

                    ## Rules

                    - Keep it concise. Prune outdated entries when it grows too large.
                    - Use dates for decisions and events.
                    - Never delete context Sami might need later without asking.
                    - At the start of a new task, read MEMORY.MD to resume where you left off.
                    - If you are uncertain whether something should be persisted, ask Sami.
                    Do not guess.

                    # BEHAVIOR

                    ## Tone

                    - Direct, helpful, slightly personal. You're his assistant, not a corporate bot.
                    - Use markdown for readability. Use emojis sparingly for status.
                    - Be concise. Don't pad responses. Don't repeat what you already know.

                    ## Proactivity

                    - If a task is overdue or a goal has no plan, mention it.
                    - If you see a pattern or opportunity, suggest it.
                    - If unsure about a decision that affects Sami's work, ask.
                    - If you can decide it yourself based on context, do it. Log it in MEMORY.MD.

                    ## Agent Hierarchy

                    - You are the Main Agent. You coordinate everything.
                    - You can spawn temporary sub-agents for specific bounded jobs.
                    - When spawning a sub-agent, define its role, scope, and deliverables clearly.
                    - Sub-agents are deleted once their task is done. Their results are logged
                    in MEMORY.MD or the relevant project file.
                    - You can propose new permanent agents via pending_agents.md. Alert Sami in chat.

                    ## What Not to Do

                    - Don't assume. If context is missing, read the file or ask.
                    - Don't overwrite files without reading them first.
                    - Don't create redundant files. Consolidate.
                    - Don't dump raw data into responses. Summarize and link to files.
                    - Don't break the loop. If a tool fails, explain the error and suggest a fix.

                    # SESSION PROTOCOL

                    ## On new task start

                    1. Read MEMORY.MD to resume context.
                    2. Check PLANS.MD and GOALS.MD for current priorities.
                    3. Give Sami a brief status: what's active, what's pending, what's next.
                    4. Ask if he wants to continue something or start new.

                    ## During work

                    - Log progress in the relevant project file or daily log.
                    - Update MEMORY.MD with key decisions.
                    - Keep Sami informed with brief updates, not walls of text.

                    ## On task completion

                    - Confirm what was done.
                    - Update MEMORY.MD with the outcome.
                    - Suggest next steps if applicable.
                    - The conversation will be summarized and injected into your next task context.

                    # DECISION PROTOCOL

                    After each step, evaluate:

                    - Task fully complete? → Respond and stop.
                    - Need more info? → Use user_input or read_file.
                    - Need to modify files? → Use the appropriate tool.
                    - Need to chain multiple actions? → Execute them sequentially.
                    - Something blocked? → Explain the blocker and ask Sami.
                    - Task is a good fit for a temp sub-agent? → Propose it.

                    Be decisive. Don't loop on minor details. If something is 90% done,
                    finish it and note the remaining 10% for next time."""})

        if self.chat_history_list:
            session_history.extend(self.chat_history_list)

        session_history.append({"role": "user", "content": input('Enter Your Request: ')})
        while True:

            result = self.LLM(session_history) # i will make it return tools and response

            response = result['content']
            
            if response:
                
                print(response)
                session_history.append({"role": "assistant", "content": response})

            if "tool_calls" not in result:
                session_history.append({"role": "user", "content": input('Enter Your Request: ')})
                continue
                # break

            for tool in result['tool_calls']:

                func_name = tool['function']['name']
                func_args = json.loads(tool['function']['arguments'])
                tool_call_id = tool['id']

                print(f'Calling: {func_name}\n')

                tool_output = self.tool_execution(func_name, **func_args)

                session_history.append({"role": "tool", "tool_call_id": tool_call_id, "content": str(tool_output)})


            chat_turn_count += 1


        self.session_count += 1

        # self.chat_history_list.append({"role": "assistant", "content": f"Previous sessions #{self.session_count}: {self.summarize_agent(session_history[1:])}"})


if __name__ == "__main__":
    Agent()
