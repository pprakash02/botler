import socket
import time

# Configuration
AMI_HOST = '127.0.0.1'
AMI_PORT = 5038
AMI_USER = 'python_user'
AMI_PASS = 'super_secret_password'

# The number you want to call (Update this to your actual endpoint/trunk)
# Example: PJSIP/15550001234@my-sip-trunk
TARGET_CHANNEL=''
def send_ami_action_login(sock, action_lines):
    payload = "\r\n".join(action_lines) + "\r\n\r\n"
    sock.sendall(payload.encode('utf-8'))
def send_ami_action(sock, action_lines):
    payload = "\r\n".join(action_lines) + "\r\n\r\n"
    sock.sendall(payload.encode('utf-8'))
    # In a real app, you would read the response here
    print(f"Sent:\n{payload}")

def initiate_call():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((AMI_HOST, AMI_PORT))
        
        # 1. Login
        login_payload = [
            'Action: Login',
            f'Username: {AMI_USER}',
            f'Secret: {AMI_PASS}'
        ]
        send_ami_action_login(s, login_payload)
        time.sleep(1) # Simple wait for login to process

        # 2. Originate the Call
        # Logic: Call 'Channel', when they answer, connect them to 'Context/Exten'
        originate_payload = [
            'Action: Originate',
            f'Channel: {TARGET_CHANNEL}',
            'Context: from-internal',
            'Exten: start_pipecat', # Matches the extension we added in Step 2
            'Priority: 1',
            'CallerID: "Botler" <999>',
            'Async: true' # Return immediately, don't wait for call to finish
        ]
        send_ami_action(s, originate_payload)
        
        print("Call initiated! Check Asterisk logs.")
        
        s.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    print("Enter destination number: ")
    TARGET_CHANNEL = 'PJSIP/'+input()
    initiate_call()
