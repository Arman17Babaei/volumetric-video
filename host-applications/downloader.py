from http.server import BaseHTTPRequestHandler,HTTPServer
import requests
import os,threading

main_ladder=[7000000,
             6500000,6000000,
             5500000,5000000,
             4500000,4300000,4000000,
             3750000,3400000,3200000,3000000,
             2800000,2500000,2250000,2000000,
             1800000,1600000,1400000,1200000,1100000,1000000,
             900000,750000,600000,500000,365000,240000,145000,90000]
def downloader(t,br):
        u1 = 'https://farzad-artemis.s3.eu-central-1.amazonaws.com/artemis1s/RtmpLiveEncoding/video/'+str(br)+'/segment_'+str(t)+'.m4s'
        # u1 = 'https://farzad-artemis.s3.eu-central-1.amazonaws.com/artemis2s/RtmpLiveEncoding/video/'+str(br)+'/segment_'+str(t)+'.m4s'

        print(u1)
        resp = requests.get(u1)
        data = resp.content
        f = open(str(br)+'/segment_'+str(t)+'.m4s', 'wb')
        f.write(data)
        f.close()

for br in main_ladder:
    os.system('mkdir  '+str(br))
    u1 = 'https://farzad-artemis.s3.eu-central-1.amazonaws.com/artemis1s/RtmpLiveEncoding/video/'+str(br)+'/init.mp4'
    # u1 = 'https://farzad-artemis.s3.eu-central-1.amazonaws.com/artemis2s/RtmpLiveEncoding/video/'+str(br)+'/init.mp4'

    resp = requests.get(u1)
    data = resp.content
    f = open(str(br) + '/init.mp4', 'wb')
    f.write(data)
    f.close()
    for s in range(0,131):
        pg = threading.Thread(target=downloader, args=(s,br))
        pg.start()


