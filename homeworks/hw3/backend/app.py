import os
import re
import json
import enum
import sqlite3
import base64
import datetime
from dotenv import load_dotenv
from flask_cors import CORS
from flask import Flask, request, jsonify, g
from langchain import hub
from langchain.chains import LLMChain
from langchain.memory import ConversationBufferMemory
from langchain.prompts import PromptTemplate
from langchain_ollama.llms import OllamaLLM
from langchain_core.tools import Tool
from langchain_google_community import GoogleSearchAPIWrapper
from langchain_openai import ChatOpenAI
from langchain.agents import create_structured_chat_agent, AgentExecutor
from libs.email_assistant import send_email
from libs.pdf_assistant import read_documents, search_documents, reload_chromadb
from libs.schedule_assistant import schedule_meeting, authenticate_google_calendar
# Load environment variables
load_dotenv()

class Task(enum.Enum):
    SEND_EMAIL = "SEND_EMAIL"
    READ_PDF = "READ_PDF"
    SCHEDULE_MEETING = "SCHEDULE_MEETING"
    SEARCH_INTERNET = "SEARCH_INTERNET"
    ASK_PRIVATE_DATA = "ASK_PRIVATE_DATA"
    NONE = "NONE"

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Set the upload folder path
UPLOAD_FOLDER = './private/docs'
DATABASE = './private/app_data.db'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


# Initialize the LLMs
local_llm = OllamaLLM(model="llama3.2")
global_llm = ChatOpenAI(name="gpt-4o-mini", temperature=0, max_tokens=256)


# Initialize the search tool
search = GoogleSearchAPIWrapper()
search_tool = Tool(
    name="google_search",
    description="Search Google for recent results.",
    func=search.run,
)
# Create an agent prompt from LangChain Hub
agent_prompt = hub.pull("hwchase17/structured-chat-agent")
# Create a structured chat agent with tools
agent = create_structured_chat_agent(
    llm=global_llm,
    tools=[search_tool],
    prompt=agent_prompt
)
# Create an AgentExecutor to manage tool usage
agent_executor = AgentExecutor(
    agent=agent,
    tools=[search_tool],
    verbose=True,
    handle_parsing_errors=True,
    max_iterations=10
)

# Create a classification prompt template
classification_prompt_template = PromptTemplate(
    input_variables=["input"],
    template=(
        "You are an AI tasked with identifying the user's intent based on their input. "
        "Classify the following input into one of the tasks: SEND_EMAIL, READ_PDF, SCHEDULE_MEETING, SEARCH_INTERNET, ASK_PRIVATE_DATA.\n\n"
        "Find the one that matches the most\n"
        "You must return in the following format:\n"
        "User Input: {input}\n"
        "Task:"
    )
)

# Initialize ConversationBufferMemory with a limited context size
chat_memory = ConversationBufferMemory(k=5)

# Set up the classification chain
classification_chain = LLMChain(llm=global_llm, prompt=classification_prompt_template)

# Set up the evaluate response chain
evaluate_response_template = PromptTemplate(
    input_variables=["input", "responseA", "responseB", "responseC", "responseD"],
    template=(
        "You are an AI tasked with evaluating the responses generated by different models. "
        "Select the response that provides the answers to the input query.\n\n"
        "Input: {input}\n\n"
        "Response A: {responseA}\n"
        "Response B: {responseB}\n"
        "Response C: {responseC}\n"
        "Response D: {responseD}\n\n"
        "Choose the one that best CONTAINS answers \n"
        "DO NOT choose based on grammar or fluency.\n"
        "You must return in the following format:\n"
        "Best Response: [A, B, C, D]\n"
        "Explanation: Explain why this response was selected."
    )
)

evaluate_response_chain = LLMChain(llm=local_llm, prompt=evaluate_response_template)

# Set up the main response chain with the LLM and memory
response_prompt_template = PromptTemplate(
    input_variables=["input"],
    template=(
        "User: {input}\n"
        "AI: Sure! I'd be happy to help. Let's get started—could you share a bit more detail so I can assist better?"
    )
)
conversation_chain = LLMChain(llm=local_llm, prompt=response_prompt_template)

analysis_template = PromptTemplate(
    input_variables=["input", "target_information"],
    template=(
        "You are an AI tasked with analyzing the following input and extracting specific target information.\n\n"
        "Input: {input}\n"
        "Target Information: {target_information}\n\n"
        "Analysis: Return the results in a structured JSON format for easy parsing. "
        "You MUST return in the following format:\n"
        "Extracted Info: {{\n"
        "  \"target_info_1\": \"<value or None>\",\n"
        "  \"target_info_2\": \"<value or None>\",\n"
        "  ...\n"
        "}}\n"
        "Ensure that your response adheres strictly to this format.\n"
    )
)
analysis_chain = LLMChain(llm=global_llm, prompt=analysis_template)

# Function to get a database connection
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
    return db
# Close the database connection after each request
@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def extract_json_from_text(text):
    """
    Extracts JSON content from a given text and parses it into a Python dictionary.

    Parameters:
    text (str): The input text containing JSON.

    Returns:
    dict: The parsed JSON as a dictionary if found, otherwise None.
    """
    # Use regex to extract JSON-like content from the text
    json_match = re.search(r"Extracted Info:\s*({.*})", text, re.DOTALL)

    if json_match:
        json_str = json_match.group(1)
        try:
            # Parse the JSON string to a Python dictionary
            extracted_info = json.loads(json_str)
            return extracted_info
        except json.JSONDecodeError as e:
            print("Error decoding JSON:", e)
            return None
    else:
        print("No JSON found in the text content.")
        return None

# Initialize the database schema
def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS personal_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                birthday TEXT,
                email TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                email TEXT,
                phone TEXT
            )
        ''')
        db.commit()

# Run the database initialization when the script is run
init_db()

def verify_email(message):
    # check if the message is an email
    email_regex = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    email_match = re.search(email_regex, message)
    if email_match:
        return email_match.group()
    return None


def search_contact(query):
    """
    Searches for a contact in the contact list based on a partial match for the name or email.

    Parameters:
    query (str): The search query to match against names and emails.

    Returns:
    list: A list of matching contacts or an empty list if no matches are found.
    """
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        'SELECT id, name, email, phone FROM contacts WHERE name LIKE ? OR email LIKE ?',
        (f'%{query}%', f'%{query}%')
    )
    results = cursor.fetchall()

    contacts = [
        {'id': row[0], 'name': row[1], 'email': row[2], 'phone': row[3]}
        for row in results
    ]

    print(results)
    
    return contacts[0] if len(contacts) > 0 else None


def cascade_search(query):
    response1 = search_local_data(query)
    response2 = search_docs(query)
    response3 = search_internet(query)
    response4 = normal_chat(query)

    # Evaluate the responses and select the best one

    evaluation_response = evaluate_response_chain.invoke({
        "input": query,
        "responseA": response1['response'],
        "responseB": response2['response'],
        "responseC": response3['response'],
        "responseD": response4['response'],
    })

    print(f"Evaluation Response: {evaluation_response}")

    regex = r"Best Response:\s*([A-D])"
    match = re.search(regex, evaluation_response["text"], re.DOTALL)
    response = None
    if match:
        best_response = match.group(1)
        if best_response == "A":
            response = {
                "task": Task.ASK_PRIVATE_DATA.name,
                "text": response1["response"]
            }
        elif best_response == "B":
            print("Best Response: PDF Documents")
            response = {
                "task": Task.READ_PDF.name,
                "text": response2["response"]
            }
        elif best_response == "C":
            print("Best Response: Internet Search")
            response = {
                "task": Task.SEARCH_INTERNET.name,
                "text": response3["response"]
            }
        else:
            response = {
                "task": Task.NONE.name,
                "text": response4["response"]
            }
    return response
    
def search_local_data(query):
    def search_personal_info(query):
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            'SELECT * FROM personal_info'
        )

        personal_info_results = cursor.fetchall()

        cursor.execute(
            'SELECT * FROM contacts'
        )

        contacts_results = cursor.fetchall()
        # use local llm to search for the query in the results
        response = conversation_chain.invoke({
            "input": (
                f"Search for the following information in the personal data: {query}.\n"
                "Here is the data:\n"
                f"Personal Info: {personal_info_results}\n"
                f"Contacts: {contacts_results}"
            )
        })

        return response

    
    response = search_personal_info(query)
    return {"source": "ASK_PRIVATE_DATA", "response": response['text']}

def search_docs(query):
    # Use the PDF assistant to search for documents
    response = search_documents(query, n_results=5)
    return {"source": "READ_PDF", "response": response}

def search_internet(query):
    try:
        agent_response = agent_executor.invoke({"input": query})
        response = agent_response
        return {"source": "SEARCH_INTERNET", "response": response['output']}
    except Exception as e:
        print(f"Error searching the internet: {e}")
        return {"source": "SEARCH_INTERNET", "response": "I'm sorry, you are currently out of limit for the day. Please try again tomorrow."}

def normal_chat(query):
    response = conversation_chain({"input": query})
    return {"source": "NONE", "response": response['text']}
# API endpoint for interacting with the structured agent
@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_input = data.get("input")

    if not user_input:
        return jsonify({"error": "Input is required"}), 400

    try:
        # Classify the task using the LLM
        task_response = classification_chain.invoke({"input": user_input})
        print(f"Task Response: {task_response}")
        task = Task.NONE
        for task_type in Task:
            if task_type.value in task_response["text"]:
                task = task_type
                break
            
        if task == Task.SEND_EMAIL:
            response = analysis_chain.invoke({"input": user_input, "target_information": "email, subject, body"})
            json_data = extract_json_from_text(response["text"])
            email = verify_email(json_data['email'])
            print(json_data)
            if email is None:
                email = search_contact(json_data['email'])
                if email is not None:
                    email = email['email']
            subject = None if "none" in json_data['subject'].lower() else json_data['subject']
            body = None if "none" in json_data['body'].lower() else json_data['body']
            
            response = {
                "email": email,
                "subject": subject,
                "body": body
            }
        elif task == Task.SCHEDULE_MEETING:
            response = conversation_chain.invoke({"input": user_input})
        else:
            response = cascade_search(user_input)
            task = Task[response['task']] if response is not None else Task.NONE

        print(f"Task: {task.name}")
        print(f"Response: {response}")
        
        return jsonify({
            "task": task.name,
            "response": response
        }), 200
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/send_email', methods=['POST'])
def send_email_task():
    data = request.json
    if not data or 'to' not in data or 'subject' not in data or 'body' not in data:
        return jsonify({"error": "Invalid data provided"}), 400

    to_addr = data['to']
    subject = data['subject']
    body = data['body']

    send_email(to_addr, subject, body)
    return jsonify({"message": "Email sent successfully"}), 200

@app.route('/upload_pdf', methods=['POST'])
def upload_pdf():
    data = request.json
    if not data or 'file' not in data:
        return jsonify({"error": "No file data provided"}), 400

    file_data = data['file']
    file_name = data.get('fileName', 'uploaded.pdf')

    file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_name)

    try:
        with open(file_path, "wb") as file:
            file.write(base64.b64decode(file_data))
        # Read the documents and store them in the database
        collection = read_documents(reload=True, collection_name="docs")
        
        documents = collection.get()["documents"]
        # Prepare data to feed into the LLM
        combined_text = "\n\n".join(documents)
        response = conversation_chain.invoke({"input": f"Read the document and summarize the content. {combined_text}"})

        return jsonify({"message": "PDF uploaded successfully", "path": file_path, "response": response}), 200
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/schedule_meeting', methods=['POST'])
def schedule_meeting_task():
    try:
        # Extract data from the POST request
        data = request.json
        summary = data.get('summary')
        location = data.get('location', '')
        description = data.get('description', '')
        start_time = data.get('start_time')
        end_time = data.get('end_time')

        if not all([summary, start_time, end_time]):
            return jsonify({'error': 'Missing required fields: summary, start_time, and end_time'}), 400

        # Convert start_time and end_time to datetime objects
        start_time = datetime.datetime.fromisoformat(start_time)
        end_time = datetime.datetime.fromisoformat(end_time)

        # Get the Google Calendar service
        service = authenticate_google_calendar()

        # Create the event
        event = schedule_meeting(service, summary, location, description, start_time, end_time)

        return jsonify({
            'message': 'Meeting scheduled successfully',
            'event_link': event
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/clear_history', methods=['POST'])
def clear_history():
    # Clear files from the upload folder
    for file in os.listdir(app.config['UPLOAD_FOLDER']):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file)
        os.remove(file_path)

    # Clear the conversation memory
    chat_memory.clear()

   # Clear the ChromaDB
    try:
        reload_chromadb()
        print("ChromaDB cleared successfully.")
    except Exception as e:
        print(f"Error clearing ChromaDB: {e}")
        return jsonify({"error": f"Error clearing ChromaDB: {str(e)}"}), 500
    return jsonify({"message": "All documents and conversation history cleared"}), 200

@app.route('/get_personal_info', methods=['GET'])
def get_personal_info():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT first_name, last_name, phone, birthday, email FROM personal_info ORDER BY id DESC LIMIT 1')
    result = cursor.fetchone()
    if result:
        personal_info = {
            'firstName': result[0],
            'lastName': result[1],
            'phone': result[2],
            'birthday': result[3],
            'email': result[4]
        }
        return jsonify({'personalInfo': personal_info}), 200
    else:
        return jsonify({'personalInfo': None}), 404

@app.route('/update_personal_info', methods=['POST'])
def update_personal_info():
    data = request.json
    first_name = data.get('firstName')
    last_name = data.get('lastName')
    phone = data.get('phone')
    birthday = data.get('birthday')
    email = data.get('email')

    db = get_db()
    cursor = db.cursor()
    cursor.execute('INSERT INTO personal_info (first_name, last_name, phone, birthday, email) VALUES (?, ?, ?, ?, ?)',
                   (first_name, last_name, phone, birthday, email))
    db.commit()
    return jsonify({'message': 'Personal information updated successfully'}), 200

@app.route('/get_contacts', methods=['GET'])
def get_contacts():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, email, phone FROM contacts')
    contacts = [{'id': row[0], 'name': row[1], 'email': row[2], 'phone': row[3]} for row in cursor.fetchall()]
    return jsonify({'contacts': contacts}), 200

@app.route('/add_contact', methods=['POST'])
def add_contact():
    data = request.json
    name = data.get('name')
    email = data.get('email')
    phone = data.get('phone')
    
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        'INSERT INTO contacts (name, email, phone) VALUES (?, ?, ?)',
        (name, email, phone)
    )
    db.commit()
    contact_id = cursor.lastrowid  # Get the ID of the newly added contact
    new_contact = {
        'id': contact_id,
        'name': name,
        'email': email,
        'phone': phone
    }
    return jsonify(new_contact), 201

@app.route('/delete_contact/<int:contact_id>', methods=['DELETE'])
def delete_contact(contact_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM contacts WHERE id = ?', (contact_id,))
    db.commit()
    if cursor.rowcount == 0:
        return jsonify({'message': 'No contact found with the given ID'}), 404
    return jsonify({'message': 'Contact deleted successfully'}), 200

@app.route('/update_contact/<int:contact_id>', methods=['PUT'])
def update_contact(contact_id):
    data = request.json
    name = data.get('name')
    email = data.get('email')
    phone = data.get('phone')

    db = get_db()
    cursor = db.cursor()
    cursor.execute('UPDATE contacts SET name = ?, email = ?, phone = ? WHERE id = ?', (name, email, phone, contact_id))
    db.commit()
    return jsonify({'message': 'Contact updated successfully'}), 200


if __name__ == '__main__':
    app.run(debug=True)
