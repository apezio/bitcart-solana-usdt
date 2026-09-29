FROM bitcart/bitcart-xmr:0.10.3.0

ENV SOL_HOST=0.0.0.0 SOL_DATA_PATH=/data
LABEL org.bitcart.image=sol-daemon

COPY --chown=electrum sol.py /home/electrum/site/daemons/sol.py

CMD ["just", "daemon", "sol"]
