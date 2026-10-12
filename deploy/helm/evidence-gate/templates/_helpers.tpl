{{- define "eg.name" -}}evidence-gate{{- end -}}

{{- define "eg.labels" -}}
app.kubernetes.io/part-of: evidence-gate
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "eg.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}

{{/* Pod-level settings shared by every workload: env, security, metrics scraping. */}}
{{- define "eg.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  fsGroup: 10001
  seccompProfile: { type: RuntimeDefault }
{{- end -}}

{{- define "eg.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: ["ALL"] }
{{- end -}}

{{- define "eg.env" -}}
envFrom:
  - configMapRef: { name: evidence-gate-config }
env:
  - name: EG_DATABASE_URL
    valueFrom: { secretKeyRef: { name: evidence-gate-secrets, key: databaseUrl } }
  - name: EG_REVIEW_TOKEN
    valueFrom: { secretKeyRef: { name: evidence-gate-secrets, key: reviewToken } }
  - name: POD_NAME
    valueFrom: { fieldRef: { fieldPath: metadata.name } }
{{- end -}}

{{- define "eg.scrape" -}}
prometheus.io/scrape: "true"
prometheus.io/port: {{ . | quote }}
prometheus.io/path: /metrics
{{- end -}}

{{/* Models: hostPath volume (kind) or an emptyDir filled from the models bucket. */}}
{{- define "eg.modelsVolume" -}}
- name: models
{{- if eq .Values.models.source "hostPath" }}
  hostPath: { path: {{ .Values.models.hostPath }}, type: Directory }
{{- else }}
  emptyDir: { sizeLimit: 4Gi }
{{- end }}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "eg.modelsInit" -}}
{{- if eq .Values.models.source "s3" }}
initContainers:
  - name: models-sync
    image: {{ include "eg.image" . }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    {{- include "eg.containerSecurity" . | nindent 4 }}
    envFrom:
      - configMapRef: { name: evidence-gate-config }
    command: ["python", "-c"]
    args:
      - |
        import os, boto3
        s3 = boto3.client("s3", endpoint_url=os.environ.get("EG_S3_ENDPOINT") or None)
        bucket, prefix = os.environ["EG_MODELS_BUCKET"], "{{ .Values.models.prefix }}"
        wanted = "{{ .Values.models.include }}".split()
        n = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix):]
                if wanted and not any(rel.startswith(w) for w in wanted):
                    continue
                dest = os.path.join("/models", rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                s3.download_file(bucket, obj["Key"], dest)
                n += 1
        print(f"synced {n} model files")
    volumeMounts:
      - { name: models, mountPath: /models }
      - { name: tmp, mountPath: /tmp }
{{- end }}
{{- end -}}
