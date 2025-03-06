import os
import torch
import torch.nn as nn
from model import SimpleDecoder
from transformers import LlamaForCausalLM, CodeLlamaTokenizer, BitsAndBytesConfig
from tokenizer import tokenizer

class CodeDataset(torch.utils.data.Dataset):
    def __init__(self, data_dir, _tokenizer, max_length=512):
        self.files = [os.path.join(data_dir, f) for f in os.listdir(data_dir)]
        self.tokenizer: CodeLlamaTokenizer = _tokenizer
        self.max_length = max_length

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        with open(self.files[idx], 'r', encoding='utf-8') as f:
            code = f.read()
            # Remove comment headers
            code = '\n'.join([line for line in code.split('\n') if not line.startswith('//')])

            if len(code) > 30000:
                # avoid super long files
                code = code[0:30000]
            
            # Tokenize and add special tokens
            inputs = self.tokenizer(
                code,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
                padding="max_length"
            )

            return {
                "input_ids": inputs["input_ids"].squeeze(),
                "attention_mask": inputs["attention_mask"].squeeze(),
                "labels": inputs["input_ids"].squeeze()
            }
            
            # # Split into chunks of max_length
            # chunks = []
            # for i in range(0, len(tokens), self.max_length):
            #     chunk = tokens[i:i+self.max_length]
            #     if len(chunk) < self.max_length:
            #         chunk += self.tokenizer.convert_tokens_to_ids(["<pad>"]) * (self.max_length - len(chunk))
            #     chunks.append(chunk)
            # return torch.tensor(chunks, dtype=torch.long)

TEMPERATURE = 0.7
ALPHA = 0.7  # Weight between teacher and ground truth loss
def distill_loss(student_logits, teacher_logits, labels):
    # Soften teacher logits with temperature
    soft_teacher = torch.nn.functional.softmax(teacher_logits / TEMPERATURE, dim=-1)
    
    # Calculate distillation loss (KL divergence)
    loss_kl = torch.nn.functional.kl_div(
        torch.nn.functional.log_softmax(student_logits / TEMPERATURE, dim=-1),
        soft_teacher,
        reduction="batchmean"
    ) * (TEMPERATURE ** 2)
    
    # Calculate standard cross-entropy loss
    loss_ce = torch.nn.functional.cross_entropy(
        student_logits.view(-1, student_logits.size(-1)),
        labels.view(-1),
        ignore_index=tokenizer.pad_token_id
    )
    
    return ALPHA * loss_kl + (1 - ALPHA) * loss_ce

def train(student, dataloader, optimizer, device, epochs=5, on_epoch_done=None):
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16
    )

    teacher = LlamaForCausalLM.from_pretrained(
        "codellama/CodeLlama-7b-hf",
        device_map="auto",
        torch_dtype=torch.float16,
        quantization_config=bnb_config
    ).eval()  # Freeze 
    # to account for added pad token
    teacher.resize_token_embeddings(len(tokenizer))

    student.train()
    
    for epoch in range(epochs):
        total_loss = 0
        for batch_idx, batch in enumerate(dataloader):
            inputs = batch["input_ids"].to(DEVICE)
            attention_mask = batch["attention_mask"].to(DEVICE)
            labels = batch["labels"].to(DEVICE)
            
            # Get teacher predictions
            with torch.no_grad():
                teacher_outputs = teacher(
                    input_ids=inputs,
                    attention_mask=attention_mask
                )
                teacher_logits = teacher_outputs.logits
            
            # Get student predictions
            student_logits = student(inputs)
            
            # Compute distillation loss
            loss = distill_loss(student_logits, teacher_logits, labels)

            loss.backward()
            optimizer.step()
            
            total_loss += loss.item()
            
            if batch_idx % 100 == 0:
                print(f"Epoch {epoch+1} | Batch {batch_idx} | Loss: {loss.item():.4f}")

        print(f"Completed Epoch {epoch+1} Average Loss: {total_loss/len(dataloader):.4f}")
            
        if on_epoch_done is not None:
            on_epoch_done(epoch, student)

# 4. Main execution
if __name__ == "__main__":
    # Config
    DATA_DIR = "data/nextjs_repos"
    MODEL_SAVE_DIR = "data/weights"
    BATCH_SIZE = 2
    SEQ_LENGTH = 512
    EPOCHS = 30
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    LEARNING_RATE = 0.0003

    # Initialize components
    dataset = CodeDataset(DATA_DIR, tokenizer, max_length=SEQ_LENGTH)

    def collate(batch):
        list_of_all = torch.tensor([], dtype=torch.long)
        for item in batch:
            list_of_all = torch.cat((list_of_all, item), 0)
        return list_of_all
    # dataloader = torch.utils.data.DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=False, collate_fn=collate)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=True)

    # Initialize model
    model = SimpleDecoder(vocab_size=len(tokenizer), max_seq_len=SEQ_LENGTH).to(DEVICE)
    model = torch.nn.DataParallel(model)

    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE)

    if not os.path.exists(MODEL_SAVE_DIR):
        os.makedirs(MODEL_SAVE_DIR)

    def on_epoch_done(epoch, model):
        # Save model
        torch.save({
            'model_state_dict': model.state_dict(),
            'tokenizer': tokenizer,
        }, os.path.join(MODEL_SAVE_DIR, f"nextjs_decoder_epoch_{epoch}.pth"))

    # Train
    train(model, dataloader, optimizer, DEVICE, epochs=EPOCHS, on_epoch_done=on_epoch_done)
