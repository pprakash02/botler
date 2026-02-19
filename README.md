# Botler: AI-Powered Customer Engagement Platform

**Project Title:** Botler: AI-Powered Customer Engagement Platform  
**Team Name:** 404_Sher_NotFound  
**Team Members:** Pranav Prakash, Priyansh Jain  

---
## 1. Demo Video ##
## 2. Setup Instructions
### Prerequisites

**Recommended Python Version:** 3.13  
**Hardware:** CUDA capable GPU

### Setup Steps

1. **Clone the repository and navigate to it:**

   ```bash
   git clone https://github.com/pprakash02/botler.git
   cd botler
   ```

2. **Create venv and Install required Python dependencies:**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate 		
   pip install -r requirements.txt
   ```
3. **Set required Environment Variables:**  
Go to [Google AI Studio](https://aistudio.google.com) and create 2 API keys (one for the Voice Agent Brain and the other for Minutes of Meeting Generator).  
   ```bash
   cd src
   ```
   Rename `env.example` to `.env` and add your keys for `GEMINI_API_KEY` and `MOM_GEMINI_API_KEY`.
4. **Telephony:**  
    Install `Asterisk 22.8.2` from source (Linux distro maintained packages may be old). Follow     the instructions given in [Asterisk Docs](https://docs.asterisk.org/Getting-Started/Installing-Asterisk/Installing-Asterisk-From-Source/).  
Navigate to `/etc/asterisk` and set the Asterisk config files as given in the `/configs` directory of `/botler`.  
(Note: You may need `sudo` privileges to change config files and run Asterisk)  
Setup your SIP client according to `pjsip.conf`. We are using UDP transport, with Username `6001` and Password `unsecurepassword` for our demo. Get the Domain for Asterisk server by running :
   ```bash
   ip addr show
   ```  
   We recommend using [Linphone](https://www.linphone.org/en/download/) as it is open-source and easy to use.

5. **Run Asterisk and Launch Botler:**
   ```bash
   sudo asterisk
   python3.13 main.py
   ```
   **For inbound:**
   You will see a prompt for `inbound/outbound:`, type `inbound` and press `Enter` to test for inbound calls. (The first run will take some time to start because `Faster Whisper` and `Piper` models will be downloaded.)   
Once you see the Tail interface on your terminal, dial any number on your Linphone client (ex: `999`) and talk to the voice agent.  
(When using the Free Tier of Gemini API key, you will be able to talk for approximately 1-2 minutes due to Rate Limits enforced by Google.)  
When you are satisfied disconnect the call, the MoM will be automatically generated and stored in `/src`.

   **For outbound:**
   You will see a prompt for `inbound/outbound:`, type `outbound` and press `Enter` to test for inbound calls. (The first run will take some time to start because `Faster Whisper` and `Piper` models will be downloaded.)   
Once you see the Tail interface on your terminal,open another terminal window and run:  
   ```bash
   cd src
   source .venv/bin/activate  #to activate venv python interpreter
   python3.13 outbound.py
   ```
     You will see a prompt for `Enter destination number:`, type `6001` and press `Enter`. You will receive a call on your Linphone client with the caller ID `Botler`.   
(When using the Free Tier of Gemini API key, you will be able to talk for approximately 1-2 minutes due to Rate Limits enforced by Google.)  
When you are satisfied disconnect the call, the MoM will be automatically generated and stored in `/src`.


---

## 3. Architecture Diagram

<img width="1440" height="1040" alt="Reference-Architecture-Pipecat" src="./assets/architecture-diagram.png" />  

The pipeline utilizes a streaming-first architecture connecting Asterisk with Pipecat. 

Audio flows from Asterisk into Pipecat, where Voice Activity Detection (VAD) handles noise suppression. The stream is transcribed by Faster-Whisper, processed by Gemini for intent, and synthesized back into audio by Piper TTS before returning to the caller. When the call is disconnected Pipecat calls functions from `summarizer.py` to create Minutes of Meeting for the call and writes them to a file.

---

## 4. Tech Stack Used

* **Language:** Python
* **Telephony:** Asterisk
* **STT:** Faster-Whisper
* **LLM:** Gemini API 
* **TTS:** Piper (`en_US-amy-medium` voice)
* **VAD:** Silero VAD
* **Orchestration:** Pipecat
* **Outbound Calling:** Asterisk Manager Interface (AMI)

---
## 5. Additional Info
   For detailed documentation regarding approach to the problem, Justification for Technical  Selection, Advantages Over Alternative solutions, Technical  Implementation Details, High Level Architectural Design, take a look at our [Project Proposal](./assets/botler-project-proposal.pdf).
