# TS3 ServerQuery 进服/离服事件 → Bark 推送
#
# 构建：  docker build -t ts3-bark:1.0.0 .
# 运行：  docker run -d --name ts3-bark --restart=unless-stopped \
#           -e TS3_HOST=192.168.100.200 -e TS3_USER=MyQueryLogin -e TS3_PASS=xxx \
#           -e BARK_SERVER=http://192.168.100.200:8880 -e BARK_KEY=xxx \
#           ts3-bark:1.0.0
#
# 调试：  docker run --rm -it ts3-bark:1.0.0 --probe     （干跑，只打印不推送）
#         docker run --rm -it ts3-bark:1.0.0 --who       （看服务器上都有谁）
FROM python:3.12-alpine

# tzdata 是为了让日志时间显示成你本地时间（alpine 默认不带时区库）
RUN apk add --no-cache tzdata

LABEL org.opencontainers.image.title="ts3-bark" \
      org.opencontainers.image.description="TeamSpeak3 ServerQuery 进服/离服事件推送到 Bark（自建 bark-server 亦可）" \
      org.opencontainers.image.source="https://github.com/sdparklab/ts3-bark" \
      org.opencontainers.image.licenses="MIT"

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app
COPY ts3_bark.py /app/ts3_bark.py

# 脚本只用 Python 标准库，没有任何第三方依赖
ENTRYPOINT ["python", "/app/ts3_bark.py"]
