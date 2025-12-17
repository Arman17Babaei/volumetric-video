#!/usr/bin/env python3
from http.server import HTTPServer, SimpleHTTPRequestHandler, test
import json
import os
import sys
import urllib

# Define the folder where logs will be saved
LOG_FOLDER = './logs'

# Ensure the log folder exists
if not os.path.exists(LOG_FOLDER):
    os.makedirs(LOG_FOLDER)

class CORSRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        # Expose here the parameters to send to the client
        self.send_header('Access-Control-Expose-Headers', 'X-Param-id, X-Param-t, X-Param-bl')        
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()
        
    def send_head(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)

        # Serve the actual file (remove query string)
        path = self.translate_path(parsed_url.path)

        if os.path.isdir(path):
            for index in ["index.html", "index.htm"]:
                index_path = os.path.join(path, index)
                if os.path.exists(index_path):
                    path = index_path
                    break
            else:
                self.send_error(403, "Directory listing not allowed")
                return None

        if not os.path.exists(path):
            self.send_error(404, "File not found")
            return None

        # START building a valid HTTP response
        self.send_response(200)

        # Set headers for CORS and custom params
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Content-Length', os.path.getsize(path))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header("Access-Control-Expose-Headers", 'X-Param-Id, X-Param-T, X-Param-Bl')

        for key, values in query_params.items():
            if values:
                self.send_header(f'X-Param-{key}', values[0])

        self.end_headers()
        # END headers

        # Open the file and return its stream
        return open(path, 'rb')

    # Handle POST request to save the log
    def do_POST(self):
        try:
            # Get the content length and read the data
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            print(f"Received POST data: {post_data.decode('utf-8')}")

            # Define the filename
            filename = f'{LOG_FOLDER}/player.json'

            # Save the data to the file
            with open(filename, 'w') as f:
                f.write(post_data.decode('utf-8'))

            # Send a JSON response
            response = {'status': 'success', 'message': f'Log saved as {filename}'}

            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
        except Exception as e:
            print(f"Error processing POST request: {e}")
            self.send_response(500)
            self.end_headers()

            # Send a JSON response
            response = {'status': 'error', 'message': 'Internal Server Error'}
            self.wfile.write(json.dumps(response).encode('utf-8'))

if __name__ == '__main__':
    # Start the server on the specified port or default to 8000
    test(CORSRequestHandler, HTTPServer, port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000)