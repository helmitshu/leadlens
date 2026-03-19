import os
import requests
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"

tavily = TavilyClient(api_key=TAVILY_API_KEY)

def search_web(query):
    results = tavily.search(query=query, max_results=5)
    content = ""
    for r in results["results"]:
        content += f"Source: {r['url']}\n{r['content']}\n\n"
    return content

def ask_ai(prompt):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    response = requests.post(GROQ_URL, headers=headers, json=body)
    result = response.json()
    if "choices" not in result:
        print("API Error:", result)
        return "Error getting response"
    return result["choices"][0]["message"]["content"]

def research_for_sales(company_name, user_name, product):
    print(f"\nSearching the web for {company_name}...")

    web_data = search_web(f"{company_name} company overview news 2024 2025")
    web_news = search_web(f"{company_name} latest news funding hiring 2025")
    web_jobs = search_web(f"{company_name} job openings careers 2025")

    print(f"Analyzing {company_name}...")

    profile = ask_ai(f"""
    You are a senior sales intelligence analyst.
    
    Here is real live web data about {company_name}:
    {web_data}
    
    Latest news:
    {web_news}

    The salesperson is {user_name} and they sell: {product}

    Based on this REAL data provide:
    1. What {company_name} does and what business problems they likely face
    2. Their industry, estimated size, and growth stage
    3. The exact decision maker title {user_name} should contact
    4. Why {company_name} would or would not need {product}
    5. Budget signals from the news — are they growing, hiring, or cutting costs
    """)

    opener = ask_ai(f"""
    Based on this real company data:
    {profile}

    Latest news about {company_name}:
    {web_news}

    {user_name} sells {product} and wants to contact {company_name}.

    Write ONE personalized opening line for a cold call or email.
    Reference something REAL and specific about {company_name}.
    Tie it directly to {product}. One sentence only.
    Sound human and confident.
    """)

    questions = ask_ai(f"""
    Based on this real company data:
    {profile}

    {user_name} sells {product} and is on a first call with {company_name}.

    Write 3 smart discovery questions to uncover if {company_name}
    needs {product} and has budget. Make them specific to what
    you know about {company_name} from the real data.
    Number them 1, 2, 3.
    """)

    objections = ask_ai(f"""
    Based on this real company data:
    {profile}

    {user_name} sells {product} to {company_name}.

    List the 3 most likely objections {company_name} will raise.
    For each write one sharp response to handle it.
    Format: Objection: ... / Response: ...
    """)

    next_steps = ask_ai(f"""
    Based on this real company data:
    {profile}

    Current job openings at {company_name}:
    {web_jobs}

    {user_name} just finished a first call with {company_name} about {product}.

    Write 3 specific recommended next steps to move this deal forward.
    Use the job posting data to identify internal priorities.
    Number them 1, 2, 3.
    """)

    return {
        "company": company_name,
        "profile": profile,
        "opener": opener,
        "questions": questions,
        "objections": objections,
        "next_steps": next_steps
    }

def save_report(results, user_name, product):
    filename = f"sales_brief_{user_name.replace(' ', '_')}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# Sales Intelligence Report\n")
        f.write(f"**Salesperson:** {user_name}\n")
        f.write(f"**Product:** {product}\n\n")
        f.write("---\n\n")

        for r in results:
            f.write(f"## {r['company']}\n\n")
            f.write(f"### Company Profile\n{r['profile']}\n\n")
            f.write(f"### Your Opening Line\n{r['opener']}\n\n")
            f.write(f"### Discovery Questions\n{r['questions']}\n\n")
            f.write(f"### Objections & How to Handle Them\n{r['objections']}\n\n")
            f.write(f"### Recommended Next Steps\n{r['next_steps']}\n\n")
            f.write("---\n\n")

    print(f"\nReport saved to {filename}")
    return filename

def run_agent():
    print("Sales Intelligence Agent")
    print("=========================\n")

    user_name = input("Your name: ").strip()
    product = input("What are you selling? Be specific: ").strip()

    print("\nEnter the companies you want to research.")
    print("Type each one and press Enter. Type 'done' when finished.\n")

    companies = []
    while True:
        company = input("Company name: ").strip()
        if company.lower() == "done":
            break
        if company:
            companies.append(company)

    if not companies:
        print("No companies entered. Exiting.")
        return

    results = []
    for company in companies:
        result = research_for_sales(company, user_name, product)
        results.append(result)
        print(f"{company} done")

    filename = save_report(results, user_name, product)
    print(f"\nDone. Open {filename} to see your sales briefs.")

run_agent()
