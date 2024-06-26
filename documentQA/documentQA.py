import os
import json
import streamlit as st
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_anthropic import ChatAnthropic
from langchain_community.vectorstores import Chroma
from langchain_community.document_loaders import CSVLoader, PyMuPDFLoader, TextLoader, UnstructuredPowerPointLoader, Docx2txtLoader, UnstructuredExcelLoader
from langchain.memory import ConversationBufferMemory
from langchain.chains import ConversationalRetrievalChain
from langchain_experimental.text_splitter import SemanticChunker
from langchain.load import dumps, loads

# Load environment variables
load_dotenv()

# Message classes
class Message:
    def __init__(self, content):
        self.content = content

class HumanMessage(Message):
    """Represents a message from the user."""
    pass

class AIMessage(Message):
    """Represents a message from the AI."""
    pass

class ChatWithFile:
    def __init__(self, file_path, file_type):
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.anthropic_api_key = os.getenv('ANTHROPIC_API_KEY')
        self.file_path = file_path
        self.file_type = file_type
        self.conversation_history = []
        self.load_file()
        self.split_into_chunks()
        self.store_in_chroma()
        self.setup_conversation_memory()
        self.setup_conversation_retrieval_chain()

    def load_file(self):
        # Use the appropriate loader based on the file type
        if self.file_type == 'csv':
            self.loader = CSVLoader(file_path=self.file_path)
        elif self.file_type == 'pdf':
            self.loader = PyMuPDFLoader(file_path=self.file_path)
        elif self.file_type == 'txt':
            self.loader = TextLoader(file_path=self.file_path)
        elif self.file_type == 'pptx':
            self.loader = UnstructuredPowerPointLoader(file_path=self.file_path)
        elif self.file_type == 'docx':
            self.loader = Docx2txtLoader(file_path=self.file_path)
        elif self.file_type == 'xlsx':
            self.loader = UnstructuredExcelLoader(file_path=self.file_path, mode="elements")                                    
        self.pages = self.loader.load_and_split()

    def split_into_chunks(self):
        self.text_splitter = SemanticChunker(OpenAIEmbeddings(), breakpoint_threshold_type="percentile")
        self.docs = self.text_splitter.split_documents(self.pages)

    def store_in_chroma(self):
        # Convert complex metadata to string
        def simplify_metadata(doc):
            if hasattr(doc, 'metadata') and isinstance(doc.metadata, dict):
                for key, value in doc.metadata.items():
                    if isinstance(value, (list, dict)):
                        doc.metadata[key] = str(value)
            return doc

        # Simplify metadata for all documents
        self.docs = [simplify_metadata(doc) for doc in self.docs]

        # Proceed with storing documents in Chroma
        embeddings = OpenAIEmbeddings()
        self.vectordb = Chroma.from_documents(self.docs, embedding=embeddings)
        self.vectordb.persist()

    def setup_conversation_memory(self):
        self.memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

    def setup_conversation_retrieval_chain(self):
        self.llm = None
        self.llm_anthropic = None

        # Only initialize OpenAI's LLM if the API key is provided
        if self.openai_api_key:
            self.llm = ChatOpenAI(temperature=0.7, model="gpt-4-1106-preview", openai_api_key=self.openai_api_key)

        # Only initialize Anthropic's LLM if the API key is provided
        if self.anthropic_api_key:
            self.llm_anthropic = ChatAnthropic(temperature=0.7, model_name="claude-3-opus-20240229", anthropic_api_key=self.anthropic_api_key)

        if self.llm:
            self.qa = ConversationalRetrievalChain.from_llm(self.llm, self.vectordb.as_retriever(search_kwargs={"k": 10}), memory=self.memory)
        if self.llm_anthropic:
            self.anthropic_qa = ConversationalRetrievalChain.from_llm(self.llm_anthropic, self.vectordb.as_retriever(search_kwargs={"k": 10}), memory=self.memory)

    def chat(self, question):
        # Generate related queries based on the initial question
        related_queries_dicts = self.generate_related_queries(question)
        # Ensure that queries are in string format, extracting the 'query' value from dictionaries
        related_queries = [q['query'] for q in related_queries_dicts]
        # Combine the original question with the related queries
        queries = [question] + related_queries

        all_results = []

        for idx, query_text in enumerate(queries):
            response = None
            # Check which language model to use based on available API keys
            if self.llm:
                response = self.qa.invoke(query_text)
            elif self.llm_anthropic:
                response = self.qa_anthropic.invoke(query_text)

            # Process the response
            if response:
                st.write("Query:", query_text)
                st.write("Response:", response['answer'])
                all_results.append({'query': query_text, 'answer': response['answer']})
            else:
                st.write("No response received for:", query_text)

        # After gathering all results, let's ask the LLM to synthesize a comprehensive answer
        if all_results:
            # Assuming reciprocal_rank_fusion is correctly applied and scored_results is prepared
            reranked_results = self.reciprocal_rank_fusion(all_results)
            # Prepare scored_results, ensuring it has the correct structure
            scored_results = [{'score': res['score'], **res['doc']} for res in reranked_results]
            synthesis_prompt = self.create_synthesis_prompt(question, scored_results)
            synthesized_response = self.llm.invoke(synthesis_prompt)
            
            if synthesized_response:
                # Assuming synthesized_response is an AIMessage object with a 'content' attribute
                st.write(synthesized_response)
                final_answer = synthesized_response.content
            else:
                final_answer = "Unable to synthesize a response."
            
            # Update conversation history with the original question and the synthesized answer
            self.conversation_history.append(HumanMessage(content=question))
            self.conversation_history.append(AIMessage(content=final_answer))

            return {'answer': final_answer}
        else:
            self.conversation_history.append(HumanMessage(content=question))
            self.conversation_history.append(AIMessage(content="No answer available."))
            return {'answer': "No results were available to synthesize a response."}

    def generate_related_queries(self, original_query):
        prompt = f"In light of the original inquiry: '{original_query}', let's delve deeper and broaden our exploration. Please construct a JSON array containing four distinct but interconnected search queries. Each query should reinterpret the original prompt's essence, introducing new dimensions or perspectives to investigate. Aim for a blend of complexity and specificity in your rephrasings, ensuring each query unveils different facets of the original question. This approach is intended to encapsulate a more comprehensive understanding and generate the most insightful answers possible. Only respond with the JSON array itself."
        response = self.llm.invoke(input=prompt)

        if hasattr(response, 'content'):
            # Directly access the 'content' if the response is the expected object
            generated_text = response.content
        elif isinstance(response, dict) and 'content' in response:
            # Extract 'content' if the response is a dict
            generated_text = response['content']
        else:
            # Fallback if the structure is different or unknown
            generated_text = str(response)
            st.error("Unexpected response format.")

        #st.write("Response content:", generated_text)

        # Assuming the 'content' starts with "content='" and ends with "'"
        # Attempt to directly parse the JSON part, assuming no other wrapping
        try:
            json_start = generated_text.find('[')
            json_end = generated_text.rfind(']') + 1
            json_str = generated_text[json_start:json_end]
            related_queries = json.loads(json_str)
            #st.write("Parsed related queries:", related_queries)
        except (ValueError, json.JSONDecodeError) as e:
            #st.error(f"Failed to parse JSON: {e}")
            related_queries = []

        return related_queries

    def retrieve_documents(self, query):
        # Example: Convert query to embeddings and perform a vector search in ChromaDB
        query_embedding = OpenAIEmbeddings()  # Assuming SemanticChunker can embed text
        search_results = self.vectordb.search(query_embedding, top_k=5)  # Adjust based on your setup
        document_ids = [result['id'] for result in search_results]  # Extract document IDs from results
        return document_ids

    def reciprocal_rank_fusion(self, all_results, k=60):
        # Assuming each result in all_results can be uniquely identified for scoring
        # And assuming all_results is directly the list you want to work with
        fused_scores = {}
        for result in all_results:
            # Let's assume you have a way to uniquely identify each result; for simplicity, use its index
            doc_id = result['query']  # or any unique identifier within each result
            if doc_id not in fused_scores:
                fused_scores[doc_id] = {"doc": result, "score": 0}
            # Example scoring adjustment; this part needs to be aligned with your actual scoring logic
            fused_scores[doc_id]["score"] += 1  # Simplified; replace with actual scoring logic

        reranked_results = sorted(fused_scores.values(), key=lambda x: x["score"], reverse=True)
        return reranked_results

    def create_synthesis_prompt(self, original_question, all_results):
        # Sort the results based on RRF score if not already sorted; highest scores first
        sorted_results = sorted(all_results, key=lambda x: x['score'], reverse=True)
        st.write("Sorted Results", sorted_results)
        prompt = f"Based on the user's original question: '{original_question}', here are the answers to the original and related questions, ordered by their relevance (with RRF scores). Please synthesize a comprehensive answer focusing on answering the original question using all the information provided below:\n\n"
        
        # Include RRF scores in the prompt, and emphasize higher-ranked answers
        for idx, result in enumerate(sorted_results):
            prompt += f"Answer {idx+1} (Score: {result['score']}): {result['answer']}\n\n"
        
        prompt += "Given the above answers, especially considering those with higher scores, please provide the best possible composite answer to the user's original question."
        
        return prompt
    
def upload_and_handle_file():
    st.title('Document QA - Chat with Document Data')
    uploaded_file = st.file_uploader("Choose a XLSX, PPTX, DOCX, PDF, CSV, or TXT file", type=["xlsx", "pptx", "docx", "pdf", "csv", "txt"])
    if uploaded_file:
        # Determine the file type and set accordingly
        if uploaded_file.name.endswith('.csv'):
            file_type = "csv"
        elif uploaded_file.name.endswith('.pdf'):
            file_type = "pdf"
        elif uploaded_file.name.endswith('.txt'):
            file_type = "txt"
        elif uploaded_file.name.endswith('.pptx'):
            file_type = "pptx"
        elif uploaded_file.name.endswith('.docx'):
            file_type = "docx"
        elif uploaded_file.name.endswith('.xlsx'):
            file_type = "xlsx"
        else:
            file_type = None  # Fallback in case of unexpected file extension

        if file_type:
            csv_pdf_txt_path = os.path.join("temp", uploaded_file.name)
            if not os.path.exists('temp'):
                os.makedirs('temp')
            with open(csv_pdf_txt_path, "wb") as f:
                f.write(uploaded_file.getvalue())
            st.session_state['file_path'] = csv_pdf_txt_path
            st.session_state['file_type'] = file_type  # Store the file type in session state
            st.success(f"{file_type.upper()} file uploaded successfully.")
            st.button("Proceed to Chat", on_click=lambda: st.session_state.update({"page": 2}))
        else:
            st.error("Unsupported file type. Please upload a XLSX, PPTX, DOCX, PDF, CSV, or TXT file.")

def chat_interface():
    st.title('Document QA - Chat with Document Data')
    file_path = st.session_state.get('file_path')
    file_type = st.session_state.get('file_type')
    if not file_path or not os.path.exists(file_path):
        st.error("File missing. Please go back and upload a file.")
        return

    if 'chat_instance' not in st.session_state:
        st.session_state['chat_instance'] = ChatWithFile(file_path=file_path, file_type=file_type)

    user_input = st.text_input("Ask a question about the document data:")
    if user_input and st.button("Send"):
        with st.spinner('Thinking...'):
            top_result = st.session_state['chat_instance'].chat(user_input)
            
            # Display the top result's answer as markdown for better readability
            if top_result:
                st.markdown("**Top Answer:**")
                st.markdown(f"> {top_result['answer']}")
            else:
                st.write("No top result available.")
                
            # Display chat history
            st.markdown("**Chat History:**")
            for message in st.session_state['chat_instance'].conversation_history:
                prefix = "*You:* " if isinstance(message, HumanMessage) else "*AI:* "
                st.markdown(f"{prefix}{message.content}")

if __name__ == "__main__":
    if 'page' not in st.session_state:
        st.session_state['page'] = 1

    if st.session_state['page'] == 1:
        upload_and_handle_file()
    elif st.session_state['page'] == 2:
        chat_interface()
