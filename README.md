# Chat-App

A real-time chat application built with Flask and SocketIO, featuring user authentication, private and public chat rooms, and an admin dashboard.

## Features

- **Real-time Messaging**: Instant messaging using WebSockets via Flask-SocketIO
- **User Authentication**: Secure login and registration system
- **Private Chat Rooms**: Create and join private rooms
- **Public Chat**: Open chat room for all users
- **Admin Dashboard**: Manage users, rooms, and system settings
- **Password Reset**: Email-based password recovery
- **Responsive Design**: Mobile-friendly interface

## Technologies Used

- **Backend**: Flask, Flask-SocketIO
- **Database**: MySQL (via PyMySQL)
- **Email**: Flask-Mail for password reset
- **Frontend**: HTML, CSS, JavaScript
- **Deployment**: Vercel

## Installation

1. **Clone the repository**:

   ```bash
   git clone https://github.com/miyasajid19/Chat-App.git
   cd Chat-App
   ```

2. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables**:
   Create a `.env` file in the root directory with the following variables:

   ```env
   FLASK_SECRET_KEY=your_secret_key_here
   FLASK_DEBUG=False
   DB_HOST=your_database_host
   DB_USER=your_database_user
   DB_PASSWORD=your_database_password
   DB_NAME=your_database_name
   DB_PORT=3306
   MAIL_SERVER=smtp.gmail.com
   MAIL_PORT=465
   MAIL_USE_SSL=True
   MAIL_USE_TLS=False
   MAIL_USERNAME=your_email@gmail.com
   MAIL_PASSWORD=your_email_password
   MAIL_DEFAULT_SENDER=your_email@gmail.com
   SOCKETIO_ASYNC_MODE=eventlet
   ```

4. **Set up the database**:
   - Create a MySQL database
   - Update the database schema (refer to the app code for table structures)

## Usage

1. **Run the application locally**:

   ```bash
   python app.py
   ```

   The app will be available at `http://localhost:5000`

2. **Deploy to Vercel**:
   - Push your code to GitHub
   - Connect your repository to Vercel
   - Vercel will automatically deploy using the `vercel.json` configuration

## Project Structure

```text
Chat-App/
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── vercel.json           # Vercel deployment configuration
├── static/               # Static files (CSS, images)
│   ├── css/
│   │   └── style.css
│   └── favicon.png
└── templates/            # HTML templates
    ├── index.html
    ├── register.html
    ├── dashboard.html
    ├── chatroom.html
    ├── public_chat.html
    ├── admin/
    │   ├── admin_dashboard.html
    │   ├── admin_users.html
    │   ├── admin_rooms.html
    │   └── admin_room_details.html
    └── includes/
        └── _flash_messages.html
```
## Interfaces
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/4568f9ad-11e5-423c-aafe-5aeb4e3a05eb" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/e3a06caa-0444-46da-9a9b-12418be670e9" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/2a8f62a6-c691-4d34-92e4-4473f7b39f85" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/b99eab36-e5c9-4c1c-8ce2-83a7035c09b8" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/ff11a8bf-d907-454f-a064-a09d1b08ec3a" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/9962d1e5-e7c5-4e04-ab9c-4a87eca2c521" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/4f575fa4-2085-42e1-986b-380a2945863a" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/619468b5-7756-40ce-b6a7-12b2cf8fe6ab" />
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/26a5bca4-a01e-4d94-9fc5-9e98db1f5f28" />
<img width="1916" height="1079" alt="image" src="https://github.com/user-attachments/assets/b8418cfa-59aa-4e2b-b1da-70b2cb593cf6" />


## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/new-feature`)
3. Commit your changes (`git commit -am 'Add new feature'`)
4. Push to the branch (`git push origin feature/new-feature`)
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
