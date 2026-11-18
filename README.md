# 💎 Kryptex Monitor Pro

Monitor autônomo, local e inteligente desenvolvido para quem minera no **Kryptex**. O software vigia a saúde do hardware, previne travamentos e **Telas Azuis (BSOD)**, gerencia ciclos de resfriamento preventivo e audita a rentabilidade real em carteira contra a estimativa teórica do benchmark.

---

## ⚡ Instalação e Uso Rápido (Plug & Play)

> **Importante:** Para utilizar o programa, você **NÃO precisa instalar Python**, bibliotecas nem configurar nada no seu sistema. **Basta baixar o arquivo executável standalone e executar!**

### 📥 Passo 1: Baixar o Executável
Baixe a versão mais recente diretamente na aba de **[Releases](../../releases/latest)**:
* 📄 **`Motor_Kryptex_GUI_PT_Panorama_Caixas.exe`**

### 🚀 Passo 2: Executar
1. Dê um duplo clique no arquivo **`Motor_Kryptex_GUI_PT_Panorama_Caixas.exe`**.
2. O Windows solicitará autorização de Administrador (o executável já possui manifesto UAC embutido). Isso é necessário para ler a telemetria da GPU via driver e encerrar processos travados caso haja risco térmico ou de tela azul.
3. Pronto! O monitor iniciará e fará a leitura automática do banco local do Kryptex.

### ⏰ Passo 3 (Opcional): Iniciar com o Windows
Se desejar que o monitor abra sozinho toda vez que você ligar o computador:
1. Deixe o arquivo **`configurar_inicializacao_windows.bat`** na mesma pasta do executável.
2. Dê um duplo clique no script `.bat`. Ele criará uma tarefa oficial no Agendador de Tarefas do Windows com privilégio elevado.

---

## 🛡️ Principais Recursos e Proteções

### 1. Escudo Anti-Tela Azul (Anti-BSOD)
* **Interceptação de TDR de GPU (`nvlddmkm`):** Identifica os primeiros avisos de recuperação de driver de vídeo no Windows antes que o sistema entre em tela azul irreversível (`0x116 VIDEO_TDR_FAILURE`).
* **Interceptação de Eventos WHEA:** Monitora falhas de paridade de barramento PCIe e hardware antes de pane fatal.
* **Proteção de VRAM e Sobrecarga Térmica:** Alerta ao ultrapassar 95% de uso de VRAM ou 82°C na GPU, forçando resfriamento se atingir 88°C.
* **Proteção contra Esgotamento de Memória RAM:** Evita travamentos por falta de memória do sistema.
* **Auto-Cooldown Preventivo:** Realiza pausas programadas (ex: 1 a 2 minutos a cada 8 horas) para liberar a VRAM e resfriar os componentes.

### 2. Auditoria Financeira de Precisão (Estimativa vs. Saldo Real)
* **Integração de Tempo Real:** Utiliza integração contínua ($\Delta t$) para calcular os ganhos minuto a minuto, sem distorções por horas parciais.
* **Controle em Dólar (USD) e Reais (BRL):** Compara a projeção teórica do Kryptex com os créditos efetivamente depositados no seu saldo em carteira.
* **Margem de Conversão Deduzida:** Permite abater a margem de conversão da receita bruta para refletir o lucro líquido real no bolso.
* **Medição de Potência Total:** Mede o consumo real da GPU (NVML) somado à CPU e periféricos, calculando o custo exato com base na sua tarifa de energia local (R$/kWh).

---

## 💻 Para Desenvolvedores (Rodando a partir do Código-Fonte)

Caso queira auditar, modificar ou executar o código Python diretamente:

1. Clone o repositório:
```bash
git clone https://github.com/SEU_USUARIO/kryptex-monitor-pro.git
cd kryptex-monitor-pro
```

2. Instale as dependências:
```bash
pip install -r requirements.txt
```

3. Execute o script principal:
```bash
python main.py
```

### Compilação de um novo executável:
Para gerar o executável autônomo com elevação UAC embutida via PyInstaller:
```bash
pyinstaller --noconfirm --onefile --noconsole --uac-admin --name "Motor_Kryptex_GUI_PT_Panorama_Caixas" --collect-all customtkinter main.py
```

---

## 📄 Requisitos do Sistema
* Windows 10 ou Windows 11 (64 bits)
* Aplicativo Desktop do Kryptex instalado e configurado
* GPU NVIDIA (para leitura nativa via `nvml.dll` / sensores de hardware)
