import streamlit as st
import google.generativeai as genai
import pandas as pd
import json

# --- Page Configuration ---
st.set_page_config(page_title="Grassi Cards: AI Prospect Parser", layout="wide")
st.title("⚾ Grassi Cards: Premium Article Parser")
st.markdown("Paste scouting reports or articles from Baseball America, FanGraphs, or Underdog below to extract target players.")

# --- API Key Input ---
# Safely take the API key via the UI so it isn't hardcoded
api_key = st.sidebar.text_input("Enter your Gemini API Key", type="password")

# --- AI Prompt Engineering ---
# This system prompt enforces your exact archetypes and the 11 Value Catalysts
SYSTEM_PROMPT = """
You are an expert sports card investment analyst. Read the provided article text and extract any players who match these criteria:
1. Target Archetypes: MLB Power/HR Hitters, NBA Wing/Guard Scorers, NFL Quarterbacks. (Ignore traditional big men, pitchers, and defense).
2. Value Catalysts: Look for players who are described as: breakout candidates, returning from injury, moving to a bigger market, primed for a bigger role, underrated next to teammates, or highly praised by analysts.

Respond ONLY with a valid JSON array of objects using this exact structure, with no markdown formatting or extra text:
[
  {
    "Player": "Full Name",
    "Sport": "MLB/NBA/NFL",
    "Catalyst": "Which of the 11 catalysts applies based on the text",
    "Reasoning": "One brief sentence summarizing why they fit the catalyst"
  }
]
"""

# --- Dashboard Layout ---
article_text = st.text_area("Paste Article Text Here:", height=300)

if st.button("Analyze Article for Targets"):
    if not api_key:
        st.error("Please enter your API Key in the sidebar.")
    elif not article_text.strip():
        st.warning("Please paste some text to analyze.")
    else:
        with st.spinner("Analyzing text for value catalysts..."):
            try:
                # Configure and call the Gemini API
                genai.configure(api_key=api_key)
                # Using Gemini 1.5 Flash as it is extremely fast and cheap for text parsing
                model = genai.GenerativeModel('gemini-1.5-flash') 
                
                # Combine the rules and the pasted text
                full_prompt = f"{SYSTEM_PROMPT}\n\nArticle Text:\n{article_text}"
                response = model.generate_content(full_prompt)
                
                # Clean the response to ensure it's pure JSON
                raw_json = response.text.replace("```json", "").replace("```", "").strip()
                extracted_data = json.loads(raw_json)
                
                if extracted_data:
                    st.success(f"Found {len(extracted_data)} target players!")
                    
                    # Convert to a Pandas DataFrame for a clean table UI
                    df = pd.DataFrame(extracted_data)
                    st.dataframe(df, use_container_width=True)
                    
                    # Ready for the next step: Exporting to the eBay API script
                    st.download_button(
                        label="Download Watchlist as CSV",
                        data=df.to_csv(index=False).encode('utf-8'),
                        file_name='target_players.csv',
                        mime='text/csv'
                    )
                else:
                    st.info("No players matching your archetypes were found in this text.")
                    
            except Exception as e:
                st.error(f"An error occurred: {e}")