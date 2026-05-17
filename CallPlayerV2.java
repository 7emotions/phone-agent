import android.media.AudioTrack;
import android.media.AudioFormat;
import android.media.AudioAttributes;
import java.io.FileInputStream;

public class CallPlayerV2 {
    public static void main(String[] args) {
        String inFile = args.length > 0 ? args[0] : "/data/local/tmp/agent_inject.wav";
        int sampleRate = 16000;
        int channelConfig = AudioFormat.CHANNEL_OUT_MONO;
        int audioFormat = AudioFormat.ENCODING_PCM_16BIT;

        AudioAttributes attrs = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .setFlags(AudioAttributes.FLAG_AUDIBILITY_ENFORCED)
            .build();

        FileInputStream fis = null;
        AudioTrack track = null;
        try {
            fis = new FileInputStream(inFile);
            byte[] hdr = new byte[44];
            fis.read(hdr);

            int bufSize = AudioTrack.getMinBufferSize(sampleRate, channelConfig, audioFormat);
            track = new AudioTrack.Builder()
                .setAudioAttributes(attrs)
                .setAudioFormat(new AudioFormat.Builder()
                    .setSampleRate(sampleRate)
                    .setChannelMask(channelConfig)
                    .setEncoding(audioFormat)
                    .build())
                .setBufferSizeInBytes(Math.max(bufSize * 2, 8192))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build();

            if (track.getState() != AudioTrack.STATE_INITIALIZED) {
                System.err.println("FAIL");
                return;
            }
            System.err.println("OK");

            track.play();
            byte[] buf = new byte[4096];
            int read, total = 0;
            while ((read = fis.read(buf)) > 0) {
                track.write(buf, 0, read);
                total += read;
            }
            System.err.println("Wrote " + total);
            track.stop();
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
        } finally {
            if (track != null) track.release();
            if (fis != null) try { fis.close(); } catch (Exception e2) {}
        }
    }
}
