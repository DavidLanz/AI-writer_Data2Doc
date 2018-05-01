"""Evaluate the model."""
import torch
from torch.autograd import Variable

from dataprepare import loaddata, data2index
from train import evaluate
from model import AttnDecoderRNN, EncoderRNN, EncoderLIN, docEmbedding, EncoderBiLSTM
from settings import file_loc, ENCODER_STYLE
from util import load_model


def hierarchical_predictwords(rt, re, rm, summary, encoder, decoder, lang, embedding_size,
                 encoder_style, beam_size):
    """The function will predict the sentecnes given boxscore.

    Encode the given box score, decode it to sentences, and then
    return the prediction and attention matrix.

    While decoding, beam search will be conducted with default beam_size as 1.

    """
    batch_length = rt.size()[0]
    input_length = rt.size()[1]
    target_length = 1000

    MAX_BLOCK, blocks_lens = find_max_block_numbers(batch_length, langs, rm)
    BLOCK_JUMPS = 32

    LocalEncoder = encoder.LocalEncoder
    GlobalEncoder = encoder.GlobalEncoder

    # For now, these are redundant
    local_encoder_outputs = Variable(torch.zeros(batch_length, input_length, embedding_size))
    local_encoder_outputs = local_encoder_outputs.cuda() if use_cuda else local_encoder_outputs
    global_encoder_outputs = Variable(torch.zeros(batch_length, MAX_BLOCK, embedding_size))
    global_encoder_outputs = global_encoder_outputs.cuda() if use_cuda else global_encoder_outputs


    # Encoding
    if encoder_style == 'BiLSTM':
        init_hidden = encoder.initHidden(batch_length)
        encoder_hidden, encoder_hiddens = encoder(rt, re, rm, init_hidden)

        # Store memory information
        for ei in range(input_length):
            encoder_outputs[:, ei] = encoder_hiddens[:, ei]

    else:
        # Local Encoder set up
        init_local_hidden = LocalEncoder.initHidden(batch_length)
        local_out, local_hidden = LocalEncoder({"rt": rt, "re": re, "rm": rm},
                                               init_local_hidden)
        # Global Encoder setup
        global_input = Variable(torch.zeros(MAX_BLOCK, batch_length,
                                            embedding_size))
        global_input = global_input.cuda() if use_cuda else global_input
        for ei in range(input_length):
            if ei % BLOCK_JUMPS == 0:
                # map ei to block number
                global_input[int(ei / (BLOCK_JUMPS + 1)), :, :] = local_out[ei, :, :]

        init_global_hidden = GlobalEncoder.initHidden(batch_length)
        global_out, global_hidden = GlobalEncoder({"local_hidden_states":
                                                  global_input}, init_global_hidden)
        """
        Store memory information
        Unify dimension: (batch, sequence length, hidden size)
        """
        local_encoder_outputs = local_out.permute(1, 0, 2)
        global_encoder_outputs = global_out.permute(1, 0, 2)

    # The decoder init for developing
    global_decoder = decoder.global_decoder
    local_decoder = decoder.local_decoder

    # Currently, we pad all box-scores to be the same length and blocks
    blocks_len = blocks_lens[0]

    # decoder starts
    gnh = global_decoder.initHidden(batch_length)
    lnh = local_decoder.initHidden(batch_length)

    g_input = global_encoder_outputs[:, -1]
    l_input = Variable(torch.LongTensor(batch_length).zero_(), requires_grad=False)
    l_input = l_input.cuda() if use_cuda else l_input

    decoder_attentions = torch.zeros(target_length, input_length)

    # Initialize the Beam
    # Each Beam cell contains [prob, route, decoder_hidden, atten]
    beams = [[0, [SOS_TOKEN], encoder_hidden, decoder_attentions]]

    # For each step
    for di in range(target_length):

        # For each information in the beam
        q = PriorityQueue()
        for beam in beams:

            prob, route, decoder_hidden, atten = beam
            destination = len(route) - 1

            # Get the lastest predecition
            decoder_input = route[-1]

            # If <EOS>, do not search for it
            if decoder_input == EOS_TOKEN:
                q.push(beam, prob)
                continue

            decoder_input = Variable(torch.LongTensor([decoder_input]))
            decoder_input = decoder_input.cuda() if use_cuda else decoder_input

            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs)

            # Get the attention vector at each prediction
            atten[destination, :decoder_attention.shape[2]] = decoder_attention.data[0, 0, :]

            # decode the word
            topv, topi = decoder_output.data.topk(beam_size)

            for i in range(beam_size):
                p = topv[0][i]
                idp = topi[0][i]
                new_beam = [prob + p, route + [idp], decoder_hidden, atten]
                q.push(new_beam, new_beam[0])

        # Keep the highest K probability
        beams = [q.pop() for i in range(beam_size)]

        # If the highest one is finished, we take that.
        if beams[0][1][-1] == 1:
            break

    # Get decoded_words and decoder_attetntions
    decoded_words = [lang.index2word[w] for w in beams[0][1][1:]]
    decoder_attentions = beams[0][3]
    return decoded_words, decoder_attentions[:len(decoded_words)]


def predictwords(rt, re, rm, summary, encoder, decoder, lang, embedding_size,
                 encoder_style, beam_size):
    """The function will predict the sentecnes given boxscore.

    Encode the given box score, decode it to sentences, and then
    return the prediction and attention matrix.

    While decoding, beam search will be conducted with default beam_size as 1.

    """
    batch_length = rt.size()[0]
    input_length = rt.size()[1]
    target_length = 1000

    encoder_outputs = Variable(torch.zeros(batch_length, input_length, embedding_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs

    # Encoding
    if encoder_style == 'BiLSTM':
        init_hidden = encoder.initHidden(batch_length)
        encoder_hidden, encoder_hiddens = encoder(rt, re, rm, init_hidden)

        # Store memory information
        for ei in range(input_length):
            encoder_outputs[:, ei] = encoder_hiddens[:, ei]

    else:
        encoder_hidden = encoder.initHidden(batch_length)
        out, encoder_hidden = encoder(rt, re, rm, encoder_hidden)

        # Store memory information
        encoder_outputs = out.permute(1, 0, 2)

        encoder_hidden = out[-1, :]

    decoder_attentions = torch.zeros(target_length, input_length)

    # Initialize the Beam
    # Each Beam cell contains [prob, route, decoder_hidden, atten]
    beams = [[0, [SOS_TOKEN], encoder_hidden, decoder_attentions]]

    # For each step
    for di in range(target_length):

        # For each information in the beam
        q = PriorityQueue()
        for beam in beams:

            prob, route, decoder_hidden, atten = beam
            destination = len(route) - 1

            # Get the lastest predecition
            decoder_input = route[-1]

            # If <EOS>, do not search for it
            if decoder_input == EOS_TOKEN:
                q.push(beam, prob)
                continue

            decoder_input = Variable(torch.LongTensor([decoder_input]))
            decoder_input = decoder_input.cuda() if use_cuda else decoder_input
            decoder_output, decoder_hidden, decoder_context, decoder_attention, pgen = decoder(
                decoder_input, decoder_hidden, encoder_outputs)

            if decoder.copy:
                decoder_output = decoder_output.exp()
                prob = Variable(torch.zeros(decoder_output.shape), requires_grad=False)
                prob = prob.cuda() if use_cuda else prob
                for i in range(decoder_attention.shape[2]):
                    prob[:,rm[:,i]] += (1-pgen)*decoder_attention[:,0,i]

                decoder_output_new = decoder_output + prob
                decoder_output_new = decoder_output_new.log()
            else:
                decoder_output_new = decoder_output

            # Get the attention vector at each prediction
            atten[destination, :decoder_attention.shape[2]] = decoder_attention.data[0, 0, :]

            # decode the word
            topv, topi = decoder_output.data.topk(beam_size)

            #TODO 

            for i in range(beam_size):
                p = topv[0][i]
                idp = topi[0][i]
                new_beam = [prob + p, route + [idp], decoder_hidden, atten]
                q.push(new_beam, new_beam[0])

        # Keep the highest K probability
        beams = [q.pop() for i in range(beam_size)]

        # If the highest one is finished, we take that.
        if beams[0][1][-1] == 1:
            break

    # Get decoded_words and decoder_attetntions
    decoded_words = [lang.index2word[w] for w in beams[0][1][1:]]
    decoder_attentions = beams[0][3]
    return decoded_words, decoder_attentions[:len(decoded_words)]


def evaluate(encoder, decoder, valid_set, lang,
             embedding_size, encoder_style=ENCODER_STYLE, iter_time=10,
             beam_size=1, verbose=True):
    """The evaluate procedure."""
    # Get evaluate data
    valid_iter = data_iter(valid_set, batch_size=1, shuffle=True)
    if use_cuda:
        encoder.cuda()
        decoder.cuda()

    for iteration in range(iter_time):

        # Get data
        data, idx_data = get_batch(next(valid_iter))
        rt, re, rm, summary = idx_data

        # For Encoding
        rt = Variable(torch.LongTensor(rt))
        re = Variable(torch.LongTensor(re))
        rm = Variable(torch.LongTensor(rm))

        # For Decoding
        summary = Variable(torch.LongTensor(summary))

        if use_cuda:
            rt, re, rm, summary = rt.cuda(), re.cuda(), rm.cuda(), summary.cuda()

        # Get decoding words and attention matrix
        decoded_words, decoder_attentions = predictwords(rt, re, rm, summary,
                                                         encoder, decoder, lang,
                                                         embedding_size, encoder_style,
                                                         beam_size)

        res = ' '.join(decoded_words[:-1])
        if verbose:
            print(res)
        yield res

        # # FOR WRITING REPORTS ONLY
        # # Compare to the origin data
        # triplets, gold_summary = data[0]

        # for word in gold_summary:
        #     print(word, end=' ')
        # print(' ')

        # showAttention(triplets, decoded_words, decoder_attentions)


def main():
    # Prepare data for loading the model
    train_data, train_lang = loaddata(file_loc, 'train')

    embedding_size = 600
    langs = train_lang
    emb = docEmbedding(langs['rt'].n_words, langs['re'].n_words,
                       langs['rm'].n_words, embedding_size)
    emb.init_weights()

    encoder_src = './models/long4_encoder_2120'
    decoder_src = './models/long4_decoder_2120'

    encoder_style = None

    if 'RNN' == ENCODER_STYLE:
        encoder = EncoderRNN(embedding_size, emb)
        encoder_style = 'RNN'
    elif 'LSTM' == ENCODER_STYLE:
        encoder = EncoderBiLSTM(embedding_size, emb)
        encoder_style = 'BiLSTM'
    else:
        encoder = EncoderLIN(embedding_size, emb)
        encoder_style = 'LIN'

    decoder = AttnDecoderRNN(embedding_size, langs['summary'].n_words)

    encoder = load_model(encoder, encoder_src)
    decoder = load_model(decoder, decoder_src)

    # Load data for evaluation
    valid_data, _ = loaddata(file_loc, 'valid')
    valid_data = data2index(valid_data, train_lang)
    text_generator = evaluate(encoder, decoder, valid_data,
                              train_lang['summary'], embedding_size,
                              encoder_style=encoder_style, iter_time=2,
                              beam_size=1, verbose=False)

    # Generate Text
    for idx, text in enumerate(text_generator):
        print('Generate Summary {}:\n{}'.format(idx + 1, text))

if __name__ == '__main__':
    main()
