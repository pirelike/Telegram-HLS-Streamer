# 🚀 Telegram HLS Streamer: Your Personal Unlimited Video Cloud

[](https://www.python.org/downloads/)
[](https://opensource.org/licenses/MIT)

**Tired of limited cloud storage and buffering streams? Telegram HLS Streamer transforms your Telegram account into a powerful, personal, and unlimited video streaming server. This tool allows you to store your entire video library on Telegram and stream it seamlessly to any device with a modern web browser.**

This application is designed for both beginners and advanced users. It automates the complex process of video transcoding, uploading, and streaming. By converting videos into the highly efficient HLS (HTTP Live Streaming) format and leveraging Telegram's generous file storage, you get a cost-effective and high-performance streaming solution. With an elegant web interface, multi-bot support for incredible speed, and intelligent caching, this is the definitive tool for personal media streaming.

-----

## 🌟 Key Features

  * **Effortless HLS Streaming**: Enjoy smooth, adaptive bitrate streaming on any device, from phones to smart TVs.
  * **Unlimited Cloud Storage**: Leverage your Telegram account as a free and unlimited video storage solution.
  * **High-Speed Multi-Bot Architecture**: Distribute the upload and download workload across multiple Telegram bots for significantly faster processing and streaming.
  * **Hardware-Accelerated Transcoding**: Utilizes the power of your GPU (NVIDIA, Intel, AMD, macOS) for blazing-fast video processing, converting files in a fraction of the time.
  * **Intelligent Predictive Caching**: The server anticipates which video segments you'll need next and pre-loads them, eliminating buffering and ensuring a seamless viewing experience.
  * **Comprehensive Subtitle Support**: Full support for embedding and streaming multiple subtitle tracks.
  * **User-Friendly Web Interface**: A modern, responsive, and feature-rich web UI for easy management of your videos, settings, and server status.
  * **Batch Processing**: Upload and process entire folders of videos in one go, perfect for entire seasons of a show.
  * **Beginner-Friendly Setup**: Meticulously designed to be easy to install and use, even for those with zero coding experience.
  * **Advanced Configuration**: Fine-tune every aspect of the application, from network settings to cache behavior, directly from the web UI or a simple `.env` file.
  * **Open Source**: Freely available, transparent, and open for community contributions.

-----

## 🔧 Project Structure

The codebase has been professionally refactored for clarity, maintainability, and scalability. This clean architecture makes it easy for developers to understand, maintain, and extend the application.

```
telegram-hls-streamer/
├── src/                          # Main source code
│   ├── core/                     # Core application components (app, config)
│   ├── processing/               # Video processing, caching, and optimization
│   ├── storage/                  # Database management
│   ├── telegram/                 # Telegram bot integration and handlers
│   ├── web/                      # Web server, routes, and request handlers
│   └── utils/                    # Utility functions (networking, logging)
├── templates/                    # HTML templates for the web interface
├── main_refactored.py            # The main entry point for the application
├── requirements.txt              # A list of all necessary Python packages
└── .env (you will create this)   # Your personal configuration file
```

-----

## 🚀 Getting Started: A Step-by-Step Guide for Beginners

Follow these detailed steps to get your personal streaming server up and running. No prior coding knowledge is required\!

### Prerequisites

  * **Python 3.9 or higher**: You can download it from the [official Python website](https://www.python.org/downloads/). During installation on Windows, make sure to check the box that says "Add Python to PATH".
  * **Git**: A version control system that will help you download the project files. You can download it from the [Git website](https://git-scm.com/downloads).
  * **A Telegram Account**: You will need this to create the bots that power the streamer.

### Step 1: Download the Application

First, you need to get the application files onto your computer. The easiest way is using Git.

1.  Open your terminal (Command Prompt, PowerShell, or Terminal on macOS/Linux).
2.  Navigate to the directory where you want to store the project.
3.  Clone the repository using the following command:
    ```bash
    git clone https://github.com/pirelike/telegram-hls-streamer.git
    ```
4.  Navigate into the newly created project directory:
    ```bash
    cd telegram-hls-streamer
    ```

### Step 2: Set Up a Virtual Environment

A virtual environment is a private workspace that keeps the project's dependencies separate from your main system. This is a best practice for all Python projects.

1.  **Create the virtual environment:**
    ```bash
    python -m venv venv
    ```
2.  **Activate the virtual environment:**
      * **On Windows:**
        ```bash
        venv\Scripts\activate
        ```
      * **On macOS and Linux:**
        ```bash
        source venv/bin/activate
        ```
    You will know it's active when you see `(venv)` at the beginning of your terminal prompt.

### Step 3: Install Dependencies

With the virtual environment active, install all the necessary Python packages listed in `requirements.txt`:

```bash
pip install -r requirements.txt
```

### Step 4: Configure Your Telegram Bots

The streamer relies on Telegram bots to store and retrieve video files.

1.  **Create a Bot with @BotFather**:

      * Open your Telegram app and search for the verified `@BotFather` bot.
      * Start a chat and type `/newbot`.
      * Follow the prompts to give your bot a name and a username.
      * BotFather will provide you with a **token**. This is very important, so copy it and keep it safe.

2.  **Get Your Chat ID**:

      * Search for `@userinfobot` on Telegram.
      * Start a chat with it, and it will immediately give you your numeric **Chat ID**.

3.  **Create the `.env` Configuration File**:

      * In the root of the project directory, create a new file named `.env`.
      * Open this file in a text editor and add the following lines, replacing the placeholders with the token and chat ID you just obtained:

    <!-- end list -->

    ```env
    # Main Bot Configuration (Bot 1)
    BOT_TOKEN="your_bot_token_here"
    CHAT_ID="your_chat_id_here"
    ```

    > **For Multi-Bot Power**: To significantly increase upload and processing speed, you can add more bots. Simply repeat the process to get more tokens and add them to your `.env` file like this:

    > ```env
    > # Additional Bots
    > BOT_TOKEN_2="your_second_bot_token_here"
    > CHAT_ID_2="your_chat_id_here"
    > ```

-----

## 🖥️ Usage

### Starting the Server

Starting the server is incredibly simple. Just run the following command in your terminal (make sure your virtual environment is still active):

```bash
python main_refactored.py serve
```

You will see a confirmation that the server has started:

```
🎉 Telegram HLS Streaming Server Started!

📡 Server Details:
   • URL: http://your_public_domain:8080
   • Local: http://127.0.0.1:8080
   • Hardware Acceleration: nvenc (or your detected hardware)
   • Multi-bot support: 3 bots configured

🚀 Ready to process videos!

Press Ctrl+C to shutdown gracefully...
```

### The Web Interface

Now, open your favorite web browser and navigate to the local URL: **[http://127.0.0.1:8080](https://www.google.com/url?sa=E&source=gmail&q=http://127.0.0.1:8080)**.

You will be greeted by the main dashboard, which provides a comprehensive overview of your server's status. From here you can:

  * **Upload Videos**: Process single videos or entire folders.
  * **View Playlists**: Access and copy streaming URLs for all your processed videos.
  * **Monitor Status**: See real-time system stats, cache usage, and database information.
  * **Configure Settings**: Tweak advanced settings for the application.

### Other Useful Commands

The application comes with several command-line tools for easy management:

  * **Test Your Bots**:
    ```bash
    python main_refactored.py test-bots
    ```
  * **View Your Configuration**:
    ```bash
    python main_refactored.py config
    ```
  * **Check Application Status**:
    ```bash
    python main_refactored.py status
    ```

-----

## 🧠 How It Works

The magic behind the streamer is a simple yet powerful workflow:

1.  **Video Upload**: You upload a video file through the web interface.
2.  **HLS Conversion**: The server's `VideoProcessor` takes the file and, using FFmpeg with hardware acceleration, splits it into small `.ts` video segments and creates a `.m3u8` playlist file.
3.  **Telegram Upload**: The `RoundRobinTelegramHandler` distributes these small segments across all your configured bots and uploads them to Telegram.
4.  **Streaming**: When you want to watch the video, the server provides the `.m3u8` playlist to your video player. The player then requests the individual `.ts` segments, which are downloaded from Telegram and served to you in real-time. The `PredictiveCacheManager` works in the background to ensure the next segments are always ready for you.

-----

## 🛠️ Troubleshooting

  * **"Command not found: python"**: Make sure you have installed Python correctly and that it's added to your system's PATH.
  * **"ModuleNotFoundError"**: This usually means you either forgot to activate the virtual environment or didn't install the dependencies. Activate the `venv` and run `pip install -r requirements.txt` again.
  * **Configuration Errors**: If the server fails to start due to configuration issues, use the `python main_refactored.py config` command to review your settings. Double-check your `.env` file for any typos.
  * **Bot Errors**: Run `python main_refactored.py test-bots` to get a detailed report on which of your bots are not working and why. The most common issues are an incorrect token or chat ID.

-----

## 🤝 Contributing

This is a community-driven project, and contributions are highly welcome\! Whether you want to fix a bug, add a new feature, or improve the documentation, please feel free to open an issue or submit a pull request.

## 📜 License

This project is licensed under the MIT License. See the [LICENSE](https://www.google.com/search?q=https://github.com/pirelike/telegram-hls-streamer/blob/main/LICENSE) file for more details.
