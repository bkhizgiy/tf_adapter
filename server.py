from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import uuid
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import requests
import yaml

config.load_incluster_config()
#k8s_client = client.ApiClient()

TF_API_URL='https://api.dev.testing-farm.io'
runs = {}

results = '''<?xml version="1.0" encoding="UTF-8"?>
 <testsuites overall-result="passed">
  <properties>
   <property name="baseosci.overall-result" value="passed"/>
  </properties>
  <testsuite name="/kernel-automotive/plans/sst_filesystems/procfs/plan" result="passed" tests="14" stage="complete">
   <logs>
    <log href="https:/artifacts.osci.redhat.com/{0}/arik" name="test log"/>
    <log href="https://artifacts.osci.redhat.com/{0}" name="workdir"/>
   </logs>
  </testsuite>
 </testsuites>'''

class CustomError(Exception):
    def __init__(self, message, code):
        self.message = message
        self.code = code
        super().__init__(self.message)

class CustomHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        logging.info("do_GET was called")
        path = self.path.split("/")
        run_id = path[3] if len(path) > 3 else None
        if run_id not in runs:
           logging.info("forwarding")
           url = f"{TF_API_URL}{self.path}"
           logging.info(url)
           response = requests.get(url)
           self.send_response(response.status_code)
           self.send_header('Content-type', 'application/json')
           self.end_headers()
           self.wfile.write(response.content)
           return
        endpoint = path[2]
        if endpoint == 'requests':
            response = {}
            runStatus = fetchRun(getRunName(run_id))
            try:
                conds = runStatus['status'].get('conditions') # Succeeded -> reasons: PipelineRunPending, Running, Succeeded, Failed, Cancelled, Timeout. Status->True/False/Unknown
                if not conds:
                    response['state'] = 'new'
                    response['result'] = { 'overall': 'unknown'} # maybe need to set it to everything unless we get a final result
                else:
                    conds = conds[0]
                    condition_reason = conds['reason']
                    #TODO check the exact mappings of the OCP to TF
                    if condition_reason == 'PipelineRunPending':
                        response['state'] = 'queued'
                    elif condition_reason == 'Running':
                        response['state'] = 'running'
                    elif condition_reason == 'Completed' and conds['type'] == 'Succeeded':
                        response['state'] = 'complete' # new/queued/running/complete/error
                        response['result'] = { 'overall': 'passed' } #passed/failed/error/unknown/skipped
                    elif condition_reason == 'Failed' or condition_reason == 'Completed' and conds['type'] == 'Failed':
                        response['state'] = 'complete'
                        response['result'] = { 'overall': 'failed' }
                    elif condition_reason == 'Cancelled':
                        response['state'] = 'complete'
                        response['result'] = { 'overall': 'failed' }
                    elif condition_reason == 'Timeout':
                        response['state'] = 'complete'
                        response['result'] = { 'overall': 'failed' }          
            except:
                response['state'] = 'new'
                response['result'] = { 'overall': 'unknown'}

            response['environments_requested'] = []
            response['id'] = run_id
            response['run'] = { 'artifacts': []}
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
        elif endpoint == 'results':
            response = {}
            self.send_response(200)
            self.send_header('Content-type', 'application/xml')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
        elif endpoint == 'testing-farm':
            if path[-1] == 'results.xml':
                self.send_response(200)
                self.send_header('Content-type', 'application/xml')
                self.end_headers()
                out = results.format(run_id)
                self.wfile.write(out.encode('utf-8'))
            elif path[-1] == 'results-junit.xml':
                with open(f"/results/{run_id}/junit.xml", 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-type', 'application/xml')
                self.end_headers()
                self.wfile.write(data)
            elif path[-1] == 'arik':
                self.send_response(200)
                self.send_header('Content-type', 'plain/text')
                self.end_headers()
                self.wfile.write('automotive!'.encode('utf-8'))
        else:
            self.send_response(400)


    def do_POST(self):
        global runs
        logging.info("do_POST was called")
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data)
        logging.info(self.path)
        #pretty_data = json.dumps(data, indent=4)
        #logging.info(pretty_data)

        if self.path.split("/")[-1] == 'requests' and not 'hardware' in data['environments'][0]:
            try:
                response = self.handleRequest(data)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
            except CustomError as e:
                self.send_response(e.code)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(e.message.encode('utf-8'))
        else:
            logging.info("forwarding")
            url = f"{TF_API_URL}{self.path}"
            logging.info(url)
            response = requests.post(url, data=post_data, headers=self.headers)
            self.send_response(response.status_code)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(response.content)

    def handleRequest(self, data):
        logging.info('handling request')

        run_id = uuid.uuid4()
        run_name = getRunName(run_id)
        global runs
        runs[str(run_id)] = 'demo/' + run_name
        gitUrl = data['environments'][0]['variables'].get('CUSTOM_DISCOVER_URL', data['test']['fmf']['url'])

        pipelinerun = {
            'apiVersion': 'tekton.dev/v1',
            'kind': 'PipelineRun',
            'metadata': {'name':run_name, 'namespace': 'demo'},
            'spec': {'params': [
                {'name': 'plan-name', 'value': '^/plans/one'},
                {'name': 'test-name', 'value': 'one'},
                {'name': 'hw-target', 'value': data['environments'][0]['variables']['HW_TARGET']},
                {'name': 'testRunId', 'value': str(run_id)},
                {'name': 'testsRepo', 'value': gitUrl},
                {'name': 'board', 'value': 'rcar-29'},
                {'name': 'skipProvisioning', 'value': 'true'},
                {'name': 'clientName', 'value': 'demo'},
                ],
                'pipelineRef': {'name': 'rcar-s4-test-pipeline'},
                'taskRunTemplate': {'serviceAccountName': 'pipeline'},
                'workspaces': [
                    {'name': 'jumpstarter-client-secret', 'secret': {'secretName': 'demo-config'}},
                    {'name': 'test-results', 'persistentVolumeClaim': {'claimName': 'tmt-results'}},
                ],
            },
        }

        '''
        if 'name' in data['test']['fmf'].keys():
            pipelinerun['spec']['params'].append({'name': 'plan-name', 'value': data['test']['fmf']['name']})
        if 'test_name' in data['test']['fmf'].keys():
            pipelinerun['spec']['params'].append({'name': 'test-name', 'value': data['test']['fmf']['test_name']})
        '''
        
        #output = yaml.dump(pipelinerun, sort_keys=False)
        #logging.info(output)

        api_instance = client.CustomObjectsApi()
        response = api_instance.create_namespaced_custom_object(
            group='tekton.dev',
            version='v1',
            namespace='demo',
            plural='pipelineruns',
            body=pipelinerun,
        )
        logging.info(response)

        # Adding the run UUID to follow the request
        pipelinerun['id'] = str(run_id)
        return pipelinerun

def getRunName(run_id):
    return 'test-' + str(run_id)

def fetchRun(run_name):
    try:
        api_instance = client.CustomObjectsApi()
        return api_instance.get_namespaced_custom_object(
            group='tekton.dev',
            version='v1',
            namespace='demo',
            plural='pipelineruns',
            name=run_name
        )
    except ApiException as e:
        print("Exception when calling CustomObjectsApi->get_namespaced_custom_object: %s\n" % e)

def run(server_class=HTTPServer, handler_class=CustomHandler, port=8080):
    server_address = ('', port)
    httpd = server_class(server_address, handler_class)
    logging.info(f'Starting httpd on port {port}...')
    httpd.serve_forever()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
