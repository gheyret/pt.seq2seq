import torch
import torch.nn as nn
import torch.nn.functional as F
from .encdec import Encoder, Decoder
from const import *


class Transformer(nn.Module):
    def __init__(self, src_n_words, tgt_n_words, max_len, d_model=512, d_ff=2048, n_layers=6,
                 n_heads=8, dropout=0.1, norm_pos='after', wshare_readout_tgtemb=False,
                 wshare_srcemb_tgtemb=False, pad_idx=PAD_idx):
        """
        params:
            src_n_words
            tgt_n_words
            max_len
            d_model: model hidden dim & word embedding dim
            d_ff: ffn hidden dim
            n_layers
            n_heads
            dropout
            norm_pos: layer normalization position. `after` / `before`
            wshare_readout_tgtemb: weight sharing between readout layer and target w_emb
            wshare_srcemb_tgtemb: weight sharing between source w_emb and target w_emb
        """
        super().__init__()
        assert d_model % n_heads == 0
        assert norm_pos in ['before', 'after']

        self.pad_idx = pad_idx
        self.max_len = max_len
        self.encoder = Encoder(src_n_words, max_len, d_model, d_ff, n_layers, n_heads, dropout,
                               norm_pos)
        self.decoder = Decoder(tgt_n_words, max_len, d_model, d_ff, n_layers, n_heads, dropout,
                               norm_pos)

        #  for p in self.parameters():
        #      if p.dim() > 1:
        #          nn.init.xavier_uniform_(p)

        # weight sharing
        if wshare_readout_tgtemb:
            self.decoder.readout.weight = self.decoder.w_emb.lut.weight
            raise NotImplementedError("WIP")

        if wshare_srcemb_tgtemb:
            assert src_n_words == tgt_n_words
            self.encoder.w_emb.lut.weight = self.decoder.w_emb.lut.weight
            raise NotImplementedError("WIP")

        # pre-generate left-only mask [1, T, T]
        T = max_len + 1
        self.left_only_mask = torch.ones(T, T, dtype=torch.uint8, device='cuda').tril().unsqueeze(0)

    def forward(self, src, src_lens, tgt, tgt_lens, teacher_forcing):
        """
        src: [B, src_len]
        tgt: [B, tgt_len]
        """
        # src_mask: padding mask [B, Q, S].
        src_mask = (src != self.pad_idx).unsqueeze(1) # [B, 1, S]

        ## forward
        # encoder
        enc_out = self.encoder(src, src_mask) # [B, S, d_model]

        # decoder
        B = src.size(0)
        use_teacher_forcing = torch.rand(1).item() < teacher_forcing
        if use_teacher_forcing:
            dec_in = tgt[:, :-1] # remove eos & match length
            # tgt_mask: padding + left-only mask.
            tgt_pad_mask = (dec_in != self.pad_idx).unsqueeze(1) # [B, 1, T]
            T = dec_in.size(-1)
            tgt_mask = tgt_pad_mask & self.left_only_mask[:, :T, :T] # [B, 1, T] & [1, T, T] => [B, T, T]
            # [B, T, tgt_n_words], [B, T, H, S]
            dec_outs, attn_ws = self.decoder(enc_out, dec_in, src_mask, tgt_mask)
        else:
            # non-cache version
            dec_in = torch.full([B, 1], SOS_idx, dtype=torch.long, device='cuda')
            dec_outs = []
            attn_ws = []
            dec_max_len = tgt.size(1)-1 if tgt is not None else self.max_len+1

            for i in range(dec_max_len):
                cT = i+1 # current timestep
                tgt_mask = self.left_only_mask[:, :cT, :cT]
                # [B, cT, tgt_n_words], [B, H, cT, S]
                dec_out, attn_w = self.decoder(enc_out, dec_in, src_mask, tgt_mask)
                _, topi = dec_out[:, -1].topk(1)
                dec_in = torch.cat([dec_in, topi], dim=-1) # [B, cT+1]

            dec_outs = dec_out
            attn_ws = attn_w.mean(dim=1) # averaging on heads

        return dec_outs, attn_ws

    def generate(self, src, src_lens):
        return self.forward(src, src_lens, None, None, 0.)
