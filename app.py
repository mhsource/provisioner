
import time
import json
import boto3
from flask import Flask, render_template, request, Response, redirect, url_for

app = Flask(__name__)
cf_client = boto3.client('cloudformation')

# Estados finais para criação ou deleção
FINAL_STATES = [
    'CREATE_COMPLETE',
    'CREATE_FAILED',
    'ROLLBACK_COMPLETE',
    'ROLLBACK_FAILED',
    'UPDATE_COMPLETE',
    'UPDATE_FAILED',
    'UPDATE_ROLLBACK_COMPLETE',
    'UPDATE_ROLLBACK_FAILED',
    'DELETE_COMPLETE',
    'DELETE_FAILED'
]

@app.route('/')
def index():
    """
    Página inicial: solicita apenas o nome do tenant.
    """
    return render_template('index.html')

@app.route('/create_stack', methods=['POST'])
def create_stack():
    """
    Gera dinamicamente um template CloudFormation para criar S3 e SQS 
    e inicia a criação do stack. Redireciona para a página de eventos.
    """
    tenant = request.form['tenant'].strip()
    stack_name = f"stack-{tenant}"

    template_body = f"""
AWSTemplateFormatVersion: '2010-09-09'
Description: Cria um S3 Bucket e uma SQS Queue para o tenant {tenant}.

Resources:
  MyBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: bucket-{tenant}

  MyQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: sqs-{tenant}
"""

    try:
        cf_client.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Capabilities=['CAPABILITY_NAMED_IAM']
        )
    except Exception as e:
        return f"Erro ao criar o stack: {e}"

    # Renderiza a página de eventos
    return render_template('events.html', stack_name=stack_name)

@app.route('/stack_events/<stack_name>')
def stream_stack_events(stack_name):
    """
    Endpoint SSE: faz streaming dos eventos CloudFormation.
    Ao chegar no estado final (create/delete complete), encerra o loop e envia um evento "done".
    """

    def event_stream():
        seen_event_ids = set()

        while True:
            # Tenta descrever o stack
            try:
                stack_resp = cf_client.describe_stacks(StackName=stack_name)
                stack_status = stack_resp['Stacks'][0]['StackStatus']
            except Exception as e:
                # Se não conseguiu descrever, normalmente significa que o stack foi deletado.
                yield f"data: [ERRO] Não foi possível descrever o stack ou stack deletado: {e}\n\n"
                # Enviar também um "done" para o front-end fechar SSE
                yield f"event: done\ndata: not_found\n\n"
                break

            # Obtemos os eventos em ordem decrescente
            try:
                events_resp = cf_client.describe_stack_events(StackName=stack_name)
                stack_events = events_resp['StackEvents']
            except Exception as e:
                yield f"data: [ERRO] Falha ao obter eventos: {str(e)}\n\n"
                yield f"event: done\ndata: error\n\n"
                break

            # Enviar cada evento novo
            for event in stack_events:
                eid = event['EventId']
                if eid not in seen_event_ids:
                    seen_event_ids.add(eid)
                    timestamp = event['Timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                    logical_id = event['LogicalResourceId']
                    status = event.get('ResourceStatus', 'N/A')
                    reason = event.get('ResourceStatusReason', '')

                    msg = f"{timestamp} | {logical_id} | {status}"
                    if reason:
                        msg += f" | {reason}"

                    yield f"data: {msg}\n\n"

            # Se o stack está em estado final, listamos recursos (se existirem) e enviamos "done"
            if stack_status in FINAL_STATES:
                if stack_status != 'DELETE_COMPLETE':
                    # Listar recursos do stack (se não foi deletado)
                    try:
                        resources_resp = cf_client.describe_stack_resources(StackName=stack_name)
                        resources_info = []
                        for r in resources_resp['StackResources']:
                            resources_info.append({
                                'LogicalResourceId': r['LogicalResourceId'],
                                'PhysicalResourceId': r.get('PhysicalResourceId', ''),
                                'ResourceType': r['ResourceType'],
                                'ResourceStatus': r['ResourceStatus']
                            })

                        resources_json = json.dumps(resources_info)
                        # Evento SSE especial com os recursos criados
                        yield f"event: finalResources\ndata: {resources_json}\n\n"

                    except Exception as e:
                        yield f"data: [ERRO] Ao listar recursos: {str(e)}\n\n"

                # Enviamos a mensagem final
                yield f"data: Stack chegou ao estado final: {stack_status}\n\n"
                # Enviamos "done" para o front-end encerrar SSE
                yield f"event: done\ndata: {stack_status}\n\n"
                break

            time.sleep(3)

    return Response(event_stream(), mimetype='text/event-stream')

@app.route('/rollback_stack/<stack_name>', methods=['POST'])
def rollback_stack(stack_name):
    """
    Faz o delete_stack (rollback) e redireciona para ver os eventos de deleção
    """
    try:
        cf_client.delete_stack(StackName=stack_name)
        return redirect(url_for('create_stack_done', stack_name=stack_name))
    except Exception as e:
        return f"Erro ao iniciar rollback (delete_stack): {e}"

@app.route('/create_stack_done/<stack_name>')
def create_stack_done(stack_name):
    """
    Renderiza a mesma página de eventos, porém agora acompanhando a deleção.
    """
    return render_template('events.html', stack_name=stack_name)

if __name__ == '__main__':
    app.run(debug=True)
