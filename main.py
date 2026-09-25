import os
import requests
from fastapi import FastAPI, Request
from celery import Celery
import chromadb
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv

#organisation
load_dotenv()

#Gemini API and GitHub API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN=os.getenv("GITHUB_TOKEN")

#Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

#FastAPI
app = FastAPI()

#Celery
celapp = Celery(
    "tasks", 
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0")
)

#ChromaDB
chroma_client = chromadb.PersistentClient(path="./chroma_db")
rules_collection = chroma_client.get_or_create_collection(name="coding_guidelines")
#rules for the code that is given so AI has a stand point 
rules_collection.upsert(
    ids=["regel_1", "regel_2", "regel_3", "regel_4"],
    documents=[
        "RULE 1 (Logging): Never use `print()` in production code. Instead, always use the Python `logging` module.",
        "RULE 2 (Safety): Passwords, tokens, or API keys must never appear as plain text in the code. Always use os.getenv().",
        "RULE 3 (Dangerous functions): The functions eval() and exec() are strictly prohibited due to extreme security risks.",
        "RULE 4 (File structure): Executable Python code belongs exclusively in .py files and never in a README.md."
    ]
)

#webhook to start the code review request
@app.post("/webhook")
async def webhook(request: Request):
    payload =await request.json()
    
    #only opened and synchronize should be checked
    if payload.get("action") not in ["opened", "synchronize"]:
        return {"message": "not a relevant PR action."}
    
    print(f"Status: {payload["action"]} | PR Nummer: {payload["pull_request"]["number"]} | Repository: {payload["repository"]["name"]} | Clone-URL: {payload["repository"]["full_name"]}")
    repo_url=payload["repository"]["full_name"]
    pr_number=payload["pull_request"]["number"]

    print(f"{repo_url} PR #{pr_number} accepted.")
    process_pr_background.delay(repo_url, pr_number)
    
    return {"message": "Received webhook"}

#get the code from the github repository
def fetch_pr_code(repo_url, pr_number):
    api_url = f"https://api.github.com/repos/{repo_url}/pulls/{pr_number}"
    headers = {"Accept": "application/vnd.github.v3.diff"}
    response = requests.get(api_url, headers=headers)
    return response.text

#send the code to gemini and let him analyze
def analyze_code(code_text):
    print("Code is analyzed and scanned for security vulnerabilities.")
    results = rules_collection.query(
        query_texts=[code_text],
        n_results=2
    )
    gefundene_regeln = "\n".join(results["documents"][0])
    prompt = f"""You are a strict senior code reviewer.
    Review the following code diff for general errors AND, in particular, for violations of our internal team rules.
    If a team rule is violated, explicitly name the rule (e.g., 'Violation of RULE 1')!

    OUR TEAM RULES:
    {gefundene_regeln}

    CODE DIFF TO REVIEW:
    {code_text}

    Keep it brief and precise (max. 3-4 sentences)."""    
    
    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
    )
    return response.text

#comment on github
def post_comment_to_github(repo_url, pr_number, comment_text):
    print(f"comment upload GitHub")
    
    api_url = f"https://api.github.com/repos/{repo_url}/issues/{pr_number}/comments"

    payload = {
        "body": comment_text
    }

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    response = requests.post(api_url, headers=headers, json=payload)
    
    if response.status_code == 201:
        print(f"comment posted in PR #{pr_number}! successfully")
    else:
        print(f"Github Error: (Code {response.status_code}): {response.text}")

#Background processing so that the code is not interrupted
@celapp.task(autoretry_for=(Exception,), retry_backoff=5, max_retries=5)
def process_pr_background(repo_url, pr_number):
    print(f"Background process for PR {pr_number}")
    code = fetch_pr_code(repo_url, pr_number)
    aianswer = analyze_code(code)
    post_comment_to_github(repo_url, pr_number, aianswer)
    
    print("completed")