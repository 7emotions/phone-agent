import android.media.AudioRecord;
import android.media.AudioFormat;
import android.media.MediaRecorder;
import java.io.FileOutputStream;
import java.io.IOException;

public class CallRecorder2 {
    public static void main(String[] args) {
        int source = args.length > 0 ? Integer.parseInt(args[0]) : 7;
        String outFile = args.length > 1 ? args[1] : "/data/local/tmp/call_src" + source + ".wav";
        int sampleRate = 16000;
        int channelConfig = AudioFormat.CHANNEL_IN_MONO;
        int audioFormat = AudioFormat.ENCODING_PCM_16BIT;
        
        int minBuf = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat);
        System.err.println("Source " + source + " buf=" + minBuf);
        
        AudioRecord recorder = null;
        FileOutputStream fos = null;
        try {
            recorder = new AudioRecord(source, sampleRate, channelConfig, audioFormat, minBuf * 2);
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                System.err.println("Init FAILED! State=" + recorder.getState());
                return;
            }
            System.err.println("Recording source " + source + " for 3s...");
            
            fos = new FileOutputStream(outFile);
            int dataSize = sampleRate * 2 * 3;
            writeWavHeader(fos, sampleRate, 1, 16, dataSize);
            
            recorder.startRecording();
            byte[] buf = new byte[minBuf];
            int totalRead = 0;
            long endTime = System.currentTimeMillis() + 3000;
            while (System.currentTimeMillis() < endTime && totalRead < dataSize) {
                int read = recorder.read(buf, 0, Math.min(buf.length, dataSize - totalRead));
                if (read > 0) {
                    fos.write(buf, 0, read);
                    totalRead += read;
                } else if (read < 0) {
                    System.err.println("read error: " + read);
                    break;
                }
            }
            recorder.stop();
            System.err.println("Got " + totalRead + " bytes");
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
        } finally {
            if (recorder != null) recorder.release();
            if (fos != null) try { fos.close(); } catch (IOException e) {}
        }
    }
    
    static void writeWavHeader(FileOutputStream fos, int rate, int channels, int bits, int dataSize) throws IOException {
        int byteRate = rate * channels * bits / 8;
        byte[] header = new byte[44];
        header[0]='R';header[1]='I';header[2]='F';header[3]='F';
        int fsize = 36 + dataSize;
        header[4]=(byte)(fsize); header[5]=(byte)(fsize>>8); header[6]=(byte)(fsize>>16); header[7]=(byte)(fsize>>24);
        header[8]='W';header[9]='A';header[10]='V';header[11]='E';
        header[12]='f';header[13]='m';header[14]='t';header[15]=' ';
        header[16]=16; header[17]=0; header[18]=0; header[19]=0;
        header[20]=1; header[21]=0;
        header[22]=(byte)channels; header[23]=0;
        header[24]=(byte)rate; header[25]=(byte)(rate>>8); header[26]=(byte)(rate>>16); header[27]=(byte)(rate>>24);
        header[28]=(byte)byteRate; header[29]=(byte)(byteRate>>8); header[30]=(byte)(byteRate>>16); header[31]=(byte)(byteRate>>24);
        header[32]=(byte)(channels*bits/8); header[33]=0;
        header[34]=(byte)bits; header[35]=0;
        header[36]='d';header[37]='a';header[38]='t';header[39]='a';
        header[40]=(byte)dataSize; header[41]=(byte)(dataSize>>8); header[42]=(byte)(dataSize>>16); header[43]=(byte)(dataSize>>24);
        fos.write(header);
    }
}
