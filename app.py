"""Envio de fotos pelo Telegram com retomada automática após FloodWait.

Mantém as credenciais em config.json:
{
  "api_id": 123456,
  "api_hash": "..."
}

CSV:
Telefone,Status
"""

import asyncio
import csv
import io
import json
import os
import queue
import re
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SESSION_PATH = BASE_DIR / "telegram_session"


def carregar_config():
    if not CONFIG_PATH.is_file():
        raise RuntimeError("Arquivo config.json não encontrado.")

    dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    api_id = dados.get("api_id")
    api_hash = (dados.get("api_hash") or "").strip()

    if not isinstance(api_id, int) or api_id <= 0:
        raise RuntimeError("API ID inválido no config.json.")
    if not api_hash:
        raise RuntimeError("API Hash não informado no config.json.")

    return api_id, api_hash


def normalizar_telefone(valor):
    digits = re.sub(r"\D", "", str(valor or ""))

    if len(digits) in (10, 11):
        digits = "55" + digits

    if not 10 <= len(digits) <= 15:
        raise ValueError("Telefone inválido; use código do país + DDD + número.")

    return "+" + digits


class Table:
    def __init__(self, path):
        self.path = Path(path)

        if not self.path.is_file():
            raise ValueError("Selecione uma planilha CSV válida.")

        raw = self.path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")

        try:
            self.dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error:
            self.dialect = csv.excel

        reader = csv.DictReader(io.StringIO(text), dialect=self.dialect)
        self.fields = reader.fieldnames or []
        self.rows = list(reader)

        if not self.fields or any(None in row for row in self.rows):
            raise ValueError("CSV inválido: confira o cabeçalho e o separador.")

        self.phone = next(
            (
                f for f in self.fields
                if f.strip().lower()
                in ("telefone", "phone", "numero", "número", "celular", "telegram")
            ),
            None,
        )

        if not self.phone:
            raise ValueError(
                "A planilha precisa de uma coluna Telefone, Número, Celular ou Telegram."
            )

        self.status = next(
            (f for f in self.fields if f.strip().lower() == "status"),
            "Status",
        )

        if self.status not in self.fields:
            self.fields.append(self.status)

        for row in self.rows:
            if not (row.get(self.status) or "").strip():
                row[self.status] = "PENDENTE"

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")

        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=self.fields,
                dialect=self.dialect,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(self.rows)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, self.path)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Telegram • Enviar foto")
        self.root.geometry("760x590")

        self.events = queue.Queue()
        self.stop = threading.Event()
        self.thread = None

        self.account_phone = tk.StringVar()
        self.csv_path = tk.StringVar()
        self.image_path = tk.StringVar()

        box = ttk.Frame(root, padding=18)
        box.pack(fill="both", expand=True)

        ttk.Label(
            box,
            text="Envio de fotos pelo Telegram",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            box,
            text=(
                "Processamento contínuo, sem pausa artificial entre contatos. "
                "Se o Telegram exigir FloodWait, aguarda e retoma automaticamente."
            ),
        ).pack(anchor="w", pady=(4, 12))

        ttk.Label(
            box,
            text="Telefone da SUA conta Telegram (ex.: +5592...)",
        ).pack(anchor="w", pady=(8, 2))
        ttk.Entry(box, textvariable=self.account_phone).pack(fill="x")

        ttk.Label(box, text="Planilha CSV").pack(anchor="w", pady=(12, 2))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.csv_path).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            row, text="Selecionar", command=self.choose_csv
        ).pack(side="right", padx=(8, 0))

        ttk.Label(box, text="Foto JPG / PNG / WEBP").pack(
            anchor="w", pady=(12, 2)
        )
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.image_path).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            row, text="Selecionar", command=self.choose_image
        ).pack(side="right", padx=(8, 0))


        ttk.Label(
            box,
            text=(
                "PENDENTE → tenta localizar → envia → ENVIADO.\n"
                "Sem pausa entre contatos. FloodWait do Telegram → aguarda e retoma."
            ),
        ).pack(anchor="w", pady=8)

        row = ttk.Frame(box)
        row.pack(fill="x", pady=8)

        self.start_button = ttk.Button(row, text="Iniciar", command=self.start)
        self.start_button.pack(side="left")

        ttk.Button(row, text="Parar", command=self.stop.set).pack(
            side="left", padx=(8, 0)
        )

        self.progress = ttk.Progressbar(box)
        self.progress.pack(fill="x", pady=(8, 0))

        self.summary = ttk.Label(box, text="Pronto.")
        self.summary.pack(anchor="w", pady=8)

        self.log = tk.Text(box, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

        self.root.after(150, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def choose_csv(self):
        name = filedialog.askopenfilename(filetypes=[("Planilha CSV", "*.csv")])
        if name:
            self.csv_path.set(name)

    def choose_image(self):
        name = filedialog.askopenfilename(
            filetypes=[("Fotos", "*.jpg *.jpeg *.png *.webp")]
        )
        if name:
            self.image_path.set(name)

    def ask_from_worker(self, title, prompt, secret=False):
        resposta = queue.Queue(maxsize=1)
        self.events.put(("ask", (title, prompt, secret, resposta)))
        value = resposta.get()

        if value is None:
            raise RuntimeError("Login cancelado.")

        return value.strip()

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        try:
            api_id, api_hash = carregar_config()
            account_phone = normalizar_telefone(self.account_phone.get())

            table = Table(self.csv_path.get())
            image = Path(self.image_path.get()).resolve()

            if not image.is_file() or image.suffix.lower() not in (
                ".jpg", ".jpeg", ".png", ".webp"
            ):
                raise ValueError("Selecione uma foto JPG, JPEG, PNG ou WEBP.")


            pending = [
                row
                for row in table.rows
                if (row.get(table.status) or "").strip().upper() == "PENDENTE"
            ]

            if not pending:
                raise ValueError("Não há contatos com status PENDENTE.")

        except Exception as exc:
            messagebox.showerror("Confira os dados", str(exc))
            return

        if not messagebox.askokcancel(
            "Iniciar envio",
            f"Tentar enviar a foto para {len(pending)} contato(s) PENDENTE(S)?",
        ):
            return

        self.stop.clear()
        self.start_button.config(state="disabled")
        self.progress.config(maximum=len(pending), value=0)

        self.thread = threading.Thread(
            target=self.run_thread,
            args=(
                api_id, api_hash, account_phone,
                table, pending, str(image)
            ),
            daemon=True,
        )
        self.thread.start()

    def run_thread(
        self, api_id, api_hash, account_phone,
        table, pending, image
    ):
        try:
            asyncio.run(
                self.run_async(
                    api_id, api_hash, account_phone,
                    table, pending, image
                )
            )
        except Exception as exc:
            self.events.put(("log", "Interrompido: " + str(exc)))
        finally:
            self.events.put(("done", None))

    async def autenticar(self, client, account_phone):
        await client.connect()

        if await client.is_user_authorized():
            self.events.put(("log", "Sessão do Telegram já autenticada."))
            return

        self.events.put(
            ("log", f"Solicitando código de login para {account_phone}...")
        )

        try:
            await client.send_code_request(account_phone)
        except PhoneNumberInvalidError:
            raise RuntimeError("O telefone da conta Telegram é inválido.")

        code = self.ask_from_worker(
            "Login Telegram",
            "Digite o código recebido no Telegram:",
        )

        try:
            await client.sign_in(account_phone, code)
        except SessionPasswordNeededError:
            password = self.ask_from_worker(
                "Verificação em duas etapas",
                "Digite a senha de verificação em duas etapas:",
                secret=True,
            )
            await client.sign_in(password=password)

        self.events.put(("log", "Login concluído."))

    async def localizar_usuario(self, client, phone, indice):
        contato = InputPhoneContact(
            client_id=indice,
            phone=phone,
            first_name=f"Contato {indice}",
            last_name="",
        )

        resultado = await client(ImportContactsRequest([contato]))

        if not resultado.users:
            return None

        return resultado.users[0]

    async def aguardar_floodwait(self, segundos):
        """Respeita integralmente a espera exigida pelo Telegram."""
        segundos = max(1, int(segundos))

        self.events.put(
            (
                "log",
                f"FLOOD_WAIT: Telegram pediu pausa de {segundos} segundo(s). "
                "A retomada será automática.",
            )
        )
        self.events.put(
            ("summary", f"Pausa do Telegram: {segundos}s restantes")
        )

        restante = segundos

        while restante > 0:
            if self.stop.is_set():
                return False

            # Atualiza o log em marcos úteis, sem escrever 120 linhas.
            if restante == segundos or restante <= 10 or restante % 30 == 0:
                self.events.put(
                    ("summary", f"Pausa do Telegram: {restante}s restantes")
                )

            await asyncio.sleep(1)
            restante -= 1

        self.events.put(
            ("log", "Pausa concluída. Retomando automaticamente o mesmo contato.")
        )
        return True

    async def run_async(
        self, api_id, api_hash, account_phone,
        table, pending, image
    ):
        client = TelegramClient(str(SESSION_PATH), api_id, api_hash)

        sent = 0
        errors = 0

        try:
            await self.autenticar(client, account_phone)
            self.events.put(
                ("log", f"Processando {len(pending)} contato(s).")
            )

            for index, row in enumerate(pending, 1):
                if self.stop.is_set():
                    self.events.put(
                        ("log", "Parado. Os contatos restantes continuam PENDENTES.")
                    )
                    break

                # O while faz o MESMO contato ser retomado após um FloodWait.
                while not self.stop.is_set():
                    try:
                        phone = normalizar_telefone(row.get(table.phone))
                        self.events.put(
                            ("log", f"{phone}: procurando no Telegram...")
                        )

                        user = await self.localizar_usuario(
                            client, phone, index
                        )

                        if user is None:
                            row[table.status] = "NAO_LOCALIZADO"
                            table.save()
                            errors += 1
                            self.events.put(
                                ("log", f"{phone}: NAO_LOCALIZADO.")
                            )
                        else:
                            self.events.put(
                                ("log", f"{phone}: enviando foto...")
                            )

                            await client.send_file(user, image)

                            row[table.status] = "ENVIADO"
                            table.save()
                            sent += 1
                            self.events.put(
                                ("log", f"{phone}: ENVIADO.")
                            )

                        # Contato terminou normalmente.
                        break

                    except FloodWaitError as exc:
                        continuar = await self.aguardar_floodwait(exc.seconds)

                        if not continuar:
                            self.events.put(
                                (
                                    "log",
                                    "Parada solicitada durante o FloodWait. "
                                    "O contato atual continua PENDENTE.",
                                )
                            )
                            break

                        # Volta para o início do while e tenta o MESMO contato.
                        continue

                    except UserPrivacyRestrictedError:
                        row[table.status] = "ERRO"
                        table.save()
                        errors += 1
                        self.events.put(
                            (
                                "log",
                                f"Linha {index}: ERRO — restrição de privacidade.",
                            )
                        )
                        break

                    except Exception as exc:
                        row[table.status] = "ERRO"
                        table.save()
                        errors += 1
                        self.events.put(
                            (
                                "log",
                                f"Linha {index}: ERRO — "
                                f"{type(exc).__name__}: {str(exc)[:250]}",
                            )
                        )
                        break

                if self.stop.is_set():
                    break

                self.events.put(
                    (
                        "progress",
                        (
                            index,
                            f"Enviados: {sent} | Outros/erros: {errors} | "
                            f"Processados: {index}/{len(pending)}",
                        ),
                    )
                )


            self.events.put(
                (
                    "log",
                    f"Finalizado. Enviados: {sent}. Outros/erros: {errors}.",
                )
            )

        finally:
            await client.disconnect()

    def poll(self):
        while not self.events.empty():
            kind, value = self.events.get()

            if kind == "log":
                self.log.config(state="normal")
                self.log.insert("end", value + "\n")
                self.log.see("end")
                self.log.config(state="disabled")

            elif kind == "progress":
                self.progress.config(value=value[0])
                self.summary.config(text=value[1])

            elif kind == "summary":
                self.summary.config(text=value)

            elif kind == "ask":
                title, prompt, secret, resposta = value
                answer = simpledialog.askstring(
                    title,
                    prompt,
                    parent=self.root,
                    show="*" if secret else None,
                )
                resposta.put(answer)

            elif kind == "done":
                self.start_button.config(state="normal")

        self.root.after(150, self.poll)

    def close(self):
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno(
                "Fechar",
                "Há um envio em andamento. Deseja solicitar a parada?",
            ):
                return

            self.stop.set()
            self.root.after(400, self.close)
            return

        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
