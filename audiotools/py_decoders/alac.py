#!/usr/bin/python

from audiotools import iter_last
from audiotools.bitstream import BitstreamReader
from audiotools.pcm import from_list
from operator import concat

def log2(i):
    bits = -1
    while (i):
        bits += 1
        i >>= 1
    return bits


def sign_only(value):
    if (value == 0):
        return 0
    elif (value > 0):
        return 1
    else:
        return -1


class ALACDecoder:
    def __init__(self, filename):
        self.reader = BitstreamReader(open(filename, "rb"), 0)

        self.reader.mark()
        try:
            #locate the "alac" atom
            #which is full of required decoding parameters
            try:
                stsd = self.find_sub_atom("moov", "trak", "mdia",
                                          "minf", "stbl", "stsd")
            except KeyError:
                raise ValueError("required stsd atom not found")

            (stsd_version, descriptions) = stsd.parse("8u 24p 32u")
            (alac1,
             alac2,
             self.samples_per_frame,
             self.bits_per_sample,
             self.history_multiplier,
             self.initial_history,
             self.maximum_k,
             self.channels,
             self.sample_rate) = stsd.parse(
                #ignore much of the stuff in the "high" ALAC atom
                "32p 4b 6P 16p 16p 16p 4P 16p 16p 16p 16p 4P" +
                #and use the attributes in the "low" ALAC atom instead
                "32p 4b 4P 32u 8p 8u 8u 8u 8u 8u 16p 32p 32p 32u")

            if ((alac1 != 'alac') or (alac2 != 'alac')):
                raise ValueError("Invalid alac atom")

            #also locate the "mdhd" atom
            #which contains the stream's length in PCM frames
            self.reader.rewind()
            mdhd = self.find_sub_atom("moov", "trak", "mdia", "mdhd")
            (version, ) = mdhd.parse("8u 24p")
            if (version == 0):
                (self.total_pcm_frames,) = mdhd.parse(
                    "32p 32p 32p 32u 2P 16p")
            elif (version == 1):
                (self.total_pcm_frames,) = mdhd.parse(
                    "64p 64p 32p 64U 2P 16p")
            else:
                raise ValueError("invalid mdhd version")

            #finally, set our stream to the "mdat" atom
            self.reader.rewind()
            (atom_size, atom_name) = self.reader.parse("32u 4b")
            while (atom_name != "mdat"):
                self.reader.skip_bytes(atom_size - 8)
                (atom_size, atom_name) = self.reader.parse("32u 4b")

        finally:
            self.reader.unmark()

    def find_sub_atom(self, *atom_names):
        reader = self.reader

        for (last, next_atom) in iter_last(iter(atom_names)):
            try:
                (length, stream_atom) = reader.parse("32u 4b")
                while (stream_atom != next_atom):
                    reader.skip_bytes(length - 8)
                    (length, stream_atom) = reader.parse("32u 4b")
                if (last):
                    return reader.substream(length - 8)
                else:
                    reader = reader.substream(length - 8)
            except IOError:
                raise KeyError(next_atom)

    def read(self, bytes):
        #if the stream is exhausted, return an empty pcm.FrameList object
        if (self.total_pcm_frames == 0):
            return from_list([], self.channels, self.bits_per_sample, True)

        #otherwise, read one ALAC frameset's worth of frame data
        frameset_data = []
        frame_channels = self.reader.read(3) + 1
        while (frame_channels != 0x8):
            frameset_data.extend(self.read_frame(frame_channels))
            frame_channels = self.reader.read(3) + 1
        self.reader.byte_align()

        #recombine the multiple frames into a single set of samples
        i = 0
        total_channels = len(frameset_data)
        samples = [0] * sum(map(len, frameset_data))
        while (len(frameset_data) > 0):
            channel = frameset_data.pop(0)
            samples[i::total_channels] = channel
            i += 1

        framelist = from_list(samples, total_channels,
                              self.bits_per_sample, True)

        #deduct PCM frames from remainder
        self.total_pcm_frames -= framelist.frames

        #return samples as a pcm.FrameList object
        return framelist

    def read_frame(self, channel_count):
        """returns a list of PCM sample lists, one per channel"""

        #read the ALAC frame header
        self.reader.skip(16)
        has_sample_count = self.reader.read(1)
        uncompressed_lsb_size = self.reader.read(2)
        uncompressed = self.reader.read(1)
        if (has_sample_count):
            sample_count = self.reader.read(32)
        else:
            sample_count = self.samples_per_frame

        if (uncompressed == 1):
            #if the frame is uncompressed,
            #read the raw, interlaced samples
            samples = [self.reader.read_signed(self.bits_per_sample)
                       for i in xrange(sample_count * channel_count)]
            return [samples[i::channel_count] for i in xrange(channel_count)]
        else:
            #if the frame is compressed,
            #read the interlacing parameters
            interlacing_shift = self.reader.read(8)
            interlacing_leftweight = self.reader.read(8)

            #subframe headers
            subframe_headers = [self.read_subframe_header()
                                for i in xrange(channel_count)]

            #optional uncompressed LSB values
            if (uncompressed_lsb_size > 0):
                uncompressed_lsbs = [self.reader.read(
                        uncompressed_lsb_size * 8)
                                     for i in xrange(sample_count *
                                                     channel_count)]
            else:
                uncompressed_lsbs = []

            #and residual blocks
            residual_blocks = [self.read_residuals(
                    self.bits_per_sample -
                    (uncompressed_lsb_size * 8) +
                    channel_count - 1,
                    sample_count)
                               for i in xrange(channel_count)]

            #calculate subframe samples based on
            #subframe header's QLP coefficients and QLP shift-needed
            decoded_subframes = [self.decode_subframe(header[0],
                                                      header[1],
                                                      residuals)
                                 for (header, residuals) in
                                 zip(subframe_headers,
                                     residual_blocks)]

            #decorrelate channels according interlacing shift and leftweight
            decorrelated_channels = self.decorrelate_channels(
                decoded_subframes,
                interlacing_shift,
                interlacing_leftweight)

            #if uncompressed LSB values are present,
            #prepend them to each sample of each channel
            if (uncompressed_lsb_size > 0):
                channels = []
                for (i, channel) in enumerate(decorrelated_channels):
                    assert(len(channel) ==
                           len(uncompressed_lsbs[i::channel_count]))
                    channels.append([s << (uncompressed_lsb_size * 8) | l
                                     for (s, l) in zip(
                                channel, uncompressed_lsbs[i::channel_count])])
                return channels
            else:
                return decorrelated_channels

    def read_subframe_header(self):
        prediction_type = self.reader.read(4)
        qlp_shift_needed = self.reader.read(4)
        rice_modifier = self.reader.read(3)
        qlp_coefficients = [self.reader.read_signed(16)
                            for i in xrange(self.reader.read(5))]

        return (qlp_shift_needed, qlp_coefficients)

    def read_residuals(self, sample_size, sample_count):
        residuals = []
        history = self.initial_history
        sign_modifier = 0
        i = 0

        while (i < sample_count):
            #get an unsigned residual based on "history"
            #and on "sample_size" as a lst resort
            k = min(log2(history / (2 ** 9) + 3), self.maximum_k)

            unsigned = self.read_residual(k, sample_size) + sign_modifier

            #clear out old sign modifier, if any
            sign_modifier = 0

            #change unsigned residual to signed residual
            if (unsigned & 1):
                residuals.append(-((unsigned + 1) / 2))
            else:
                residuals.append(unsigned / 2)

            #update history based on unsigned residual
            if (unsigned <= 0xFFFF):
                history += ((unsigned * self.history_multiplier) -
                            ((history * self.history_multiplier) >> 9))
            else:
                history = 0xFFFF

            #if history gets too small, we may have a block of 0 samples
            #which can be compressed more efficiently
            if ((history < 128) and ((i + 1) < sample_count)):
                zeroes_k = min(7 -
                               log2(history) +
                               ((history + 16) / 64),
                               self.maximum_k)
                zero_residuals = self.read_residual(zeroes_k, 16)
                if (zero_residuals > 0):
                    residuals.extend([0] * zero_residuals)
                    i += zero_residuals

                history = 0

                if (zero_residuals <= 0xFFFF):
                    sign_modifier = 1

            i += 1

        return residuals

    def read_residual(self, k, sample_size):
        msb = self.reader.limited_unary(0, 9)
        if (msb is None):
            return self.reader.read(sample_size)
        elif (k == 0):
            return msb
        else:
            lsb = self.reader.read(k)
            if (lsb > 1):
                return msb * ((1 << k) - 1) + (lsb - 1)
            elif (lsb == 1):
                self.reader.unread(1)
                return msb * ((1 << k) - 1)
            else:
                self.reader.unread(0)
                return msb * ((1 << k) - 1)

    def decode_subframe(self, qlp_shift_needed, qlp_coefficients, residuals):
        samples = [residuals.pop(0)]
        for i in xrange(len(qlp_coefficients)):
            samples.append(samples[-1] + residuals.pop(0))

        for residual in residuals:
            base_sample = samples[-len(qlp_coefficients) - 1]
            lpc_sum = sum([(s - base_sample) * c for (s,c) in
                           zip(samples[-len(qlp_coefficients):],
                               reversed(qlp_coefficients))])
            outval = (1 << (qlp_shift_needed - 1)) + lpc_sum
            outval >>= qlp_shift_needed
            samples.append(outval + residual + base_sample)

            buf = samples[-len(qlp_coefficients) - 2:-1]

            #error value then adjusts the coefficients table
            if (residual > 0):
                predictor_num = len(qlp_coefficients) - 1

                while ((predictor_num >= 0) and residual > 0):
                    val = buf[0] - buf[len(qlp_coefficients) - predictor_num];
                    sign = sign_only(val)

                    qlp_coefficients[predictor_num] -= sign

                    val *= sign

                    residual -= ((val >> qlp_shift_needed) *
                                 (len(qlp_coefficients) - predictor_num))
                    predictor_num -= 1

            elif (residual < 0):
                #the same as above, but we break if residual goes positive
                predictor_num = len(qlp_coefficients) - 1

                while ((predictor_num >= 0) and residual < 0):
                    val = buf[0] - buf[len(qlp_coefficients) - predictor_num];
                    sign = -sign_only(val)

                    qlp_coefficients[predictor_num] -= sign

                    val *= sign

                    residual -= ((val >> qlp_shift_needed) *
                                 (len(qlp_coefficients) - predictor_num))
                    predictor_num -= 1

        return samples

    def decorrelate_channels(self, channel_data,
                             interlacing_shift, interlacing_leftweight):
        if (len(channel_data) != 2):
            return channel_data
        elif (interlacing_leftweight == 0):
            return channel_data
        else:
            left = []
            right = []
            for (ch1, ch2) in zip(*channel_data):
                right.append(ch1 - ((ch2 * interlacing_leftweight) /
                                    (2 ** interlacing_shift)))
                left.append(ch2 + right[-1])
            return [left, right]

    def close(self):
        pass
