FROM alpine:3.20
RUN apk add --no-cache openssh rsync bash coreutils findutils grep \
 && ssh-keygen -A \
 && adduser -D -s /bin/bash alice \
 && sed -i 's/^alice:!/alice:*/' /etc/shadow \
 && mkdir -p /home/alice/.ssh /home/alice/papers/thesis-draft /home/alice/experiments/runs /home/alice/datasets \
 && echo '\documentclass{report}\begin{document}TBD\end{document}' > /home/alice/papers/thesis-draft/thesis.tex \
 && echo 'python train.py --lr 3e-4' > /home/alice/experiments/run.sh \
 && printf 'PasswordAuthentication no\nPermitRootLogin no\n' >> /etc/ssh/sshd_config
COPY demo_key.pub /home/alice/.ssh/authorized_keys
COPY demo-paper /home/alice/papers/demo-paper
RUN chown -R alice:alice /home/alice && chmod 700 /home/alice/.ssh && chmod 600 /home/alice/.ssh/authorized_keys
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
